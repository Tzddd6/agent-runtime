from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from aios.llm_core.model_service import ModelService
from aios.scheduler.workflow_checkpoint import WorkflowCheckpoint
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole


def wait_for_job(console, job_id):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        job = console.detail(job_id)
        if job['status'] != 'running':
            return job
        sleep(.01)
    raise TimeoutError(f'Job {job_id} did not finish')


class RuntimeOptimizationsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))

    def console(self, kernel):
        console = NativeConsole(kernel, self.root / 'jobs')
        self.addCleanup(console.close)
        return console

    def test_engineering_agents_have_fresh_budgets_and_global_limit_still_applies(self):
        kernel = self.enterContext(AIOSKernelRuntime(storage_root=str(self.root / 'store')))
        console = self.console(kernel)
        drf = kernel.scheduler.drf_controller
        drf.register_agent('patcher_agent', max_model_calls=0)
        drf.global_max_model_calls = 2
        payload = {'preset': 'engineering', 'project': 'json_pointer', 'deterministic_stub': True}
        first, second, blocked = (
            wait_for_job(console, console.submit(payload)['id']) for _ in range(3)
        )

        for job in (first, second):
            self.assertEqual(job['status'], 'succeeded')
            self.assertEqual(job['agent_telemetry']['patcher_agent']['model_calls_used'], 1)
            self.assertTrue(job['agent_telemetry']['patcher_agent']['agent_id'].startswith(job['id']))
        self.assertEqual(second['global_model_budget']['calls'], {'used': 2, 'limit': 2})
        self.assertIn('全局模型额度', blocked['error'])
        self.assertEqual(blocked['model_budget']['calls'], 0)
        self.assertEqual(blocked['states']['patch'], 'failed')

    def test_single_timeout_keeps_profile_and_other_requests_active(self):
        entered, release, returned = Event(), Event(), Event()

        class SlowProvider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                entered.set()
                try:
                    if release.wait(timeout=5):
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'{"data":[{"id":"local-model"}]}')
                finally:
                    returned.set()

        server = self.enterContext(ThreadingHTTPServer(('127.0.0.1', 0), SlowProvider))
        thread = Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        service = ModelService()
        self.addCleanup(service.close)
        service.request('save', {'id': 'local', 'provider': 'vllm', 'label': 'Local', 'key': 'synthetic-profile-value',
                                 'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'model': 'local-model'})
        service.request('select', {'id': 'local'})
        service.request_timeout = .4
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(service.request, 'test_connection')
            try:
                self.assertTrue(entered.wait(timeout=2))
                self.assertTrue(service.request('status')['configured'])
                with self.assertRaises(TimeoutError):
                    pending.result(timeout=2)
                self.assertTrue(service.process.is_alive())
            finally:
                release.set()
        self.assertTrue(returned.wait(timeout=2))
        service.request_timeout = 2
        self.assertTrue(service.request('test_connection')['ok'])
        self.assertEqual(service.request('status')['model'], 'local-model')

    def test_pin_survives_reload_and_pruning_keeps_shared_context(self):
        kernel = self.enterContext(AIOSKernelRuntime(storage_root=str(self.root / 'store')))
        with patch('runtime.web.JOB_HISTORY_LIMIT', 2):
            console = self.console(kernel)
            artifact_job = wait_for_job(console, console.submit({
                'preset': 'engineering', 'project': 'json_pointer', 'deterministic_stub': True
            })['id'])
            artifact_id = artifact_job['id']
            console.pin_job(artifact_id, True)
            first = wait_for_job(console, console.submit({'preset': 'document', 'prompt': 'same value'})['id'])
            second = wait_for_job(console, console.submit({'preset': 'document', 'prompt': 'same value'})['id'])
            ref = second['context_refs']['stats']
            self.assertEqual(kernel.context_store.get(ref, first['id'] + '-stats'), first['results']['stats'])
            wait_for_job(console, console.submit({'preset': 'document', 'prompt': 'other value'})['id'])

            self.assertEqual(ref, first['context_refs']['stats'])
            self.assertIsNone(kernel.workflow_engine.load_checkpoint(first['id']))
            with self.assertRaises(PermissionError):
                kernel.context_store.get(ref, first['id'] + '-stats')
            self.assertEqual(kernel.context_store.get(ref, second['id'] + '-stats'), second['results']['stats'])
            console.close()

            restored = self.console(kernel)
            self.assertTrue(restored.detail(artifact_id)['pinned'])
            self.assertTrue(restored.download_zip(artifact_id))
            self.assertIsNotNone(kernel.workflow_engine.load_checkpoint(artifact_id))
            restored.pin_job(artifact_id, False)
            self.assertIsNone(kernel.workflow_engine.load_checkpoint(artifact_id))
            self.assertFalse((restored.root / f'{artifact_id}_delivery.zip').exists())
            with self.assertRaises(KeyError):
                restored.detail(artifact_id)

    def test_checkpoint_reads_deltas_and_legacy_snapshot(self):
        checkpoint = WorkflowCheckpoint(self.root, 'durable')
        legacy = {'workflow_id': 'durable', 'states': {'work': 'waiting'}}
        checkpoint.legacy.write_text(json.dumps(legacy), encoding='utf-8')
        self.assertEqual(checkpoint.load(), legacy)

        with patch.object(WorkflowCheckpoint, 'snapshot_interval', 2), checkpoint.writer() as writer:
            writer.plan_signature = 'expected'
            writer.snapshot({'work': 'waiting'}, {}, {}, [])
            writer.append({'work': 'running'}, {}, {}, [{'state': 'running'}], ('work',))
            self.assertEqual(checkpoint.load()['states'], {'work': 'running'})
            self.assertEqual(writer.deltas_saved, 1)
            writer.append({'work': 'succeeded'}, {'work': 42}, {'work': 'ctx_saved'},
                          [{'state': 'running'}, {'state': 'succeeded'}], ('work',))
            self.assertEqual(writer.deltas_saved, 0)
        snapshot = checkpoint.load()
        self.assertEqual(snapshot['results'], {'work': 42})
        self.assertEqual(snapshot['context_refs'], {'work': 'ctx_saved'})
        self.assertEqual(snapshot['events'], [{'state': 'running'}, {'state': 'succeeded'}])
        checkpoint.purge()
        self.assertIsNone(checkpoint.load())
