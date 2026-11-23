from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from aios.scheduler.drf_admission import ResourceVector
from cerebrum.llm.apis import LLMResponse
from cerebrum.tool.apis import ToolQuery
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole


class NativeAcceptanceTest(unittest.TestCase):
    def test_free_console_scenarios_finish_in_the_native_engine(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            try:
                for preset in ('dynamic', 'document', 'kernel', 'resources', 'agents', 'ipc', 'litecua'):
                    identity = console.submit({'preset': preset, 'prompt': 'fixture input', 'workers': 4})['id']
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        detail = console.detail(identity)
                        if detail['status'] != 'running':
                            break
                        time.sleep(0.01)
                    self.assertEqual(detail['status'], 'completed_with_failures' if preset == 'dynamic' else 'succeeded', preset)
                    self.assertTrue(detail['graph'], preset)
                    self.assertEqual(kernel.process_service.snapshot()['active'], 0)
            finally:
                console.close()

    def test_agent_resource_wait_releases_then_runs_the_next_worker(self):
        with (TemporaryDirectory() as folder,
              AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel,
              ThreadPoolExecutor(max_workers=2) as pool):
            kernel.scheduler.drf_controller.register_agent('quota', resource_limit=ResourceVector(250, 64 * 1024**2, 4))
            query = ToolQuery(tool_calls=[{'name': 'runtime.load', 'parameters': {'seconds': 0.2}}])
            futures = [pool.submit(kernel.execute_request, 'quota', query) for _ in range(2)]
            for future in futures:
                self.assertEqual(future.result(timeout=5)['response'].status_code, 200)
            self.assertEqual(len(kernel.process_service.snapshot()['receipts']), 2)
            self.assertEqual(kernel.scheduler.drf_controller.used.cpu_milli, 0)

    def test_all_controlled_projects_accept_only_verified_model_candidates(self):
        from aios.tool.engineering import manifest
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            try:
                for project in ('pricing', 'intervals', 'text_normalization'):
                    raw_json = json.dumps({'source': manifest(project)['reference_fix']})
                    fenced = f"```json\n{raw_json}\n```" if project == 'pricing' else raw_json
                    candidate = LLMResponse(response_message=fenced)
                    with patch.object(console.model_service, 'complete', return_value=candidate):
                        result = console.conversations.submit({'conversation_id': uuid4().hex, 'request_id': uuid4().hex,
                                                               'prompt': 'repair', 'mode': 'engineering', 'project': project})
                    self.assertEqual(result['status'], 'succeeded', project)
                    self.assertTrue(json.loads(result['answer'])['success'], project)
            finally:
                console.close()

    def test_model_failure_is_recorded_without_paid_retry(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            try:
                with patch.object(console.model_service, 'complete', side_effect=RuntimeError('synthetic transport failure')) as model:
                    result = console.conversations.submit({'conversation_id': uuid4().hex, 'request_id': uuid4().hex,
                                                           'prompt': 'fixture', 'mode': 'llm'})
                self.assertEqual(model.call_count, 1)
                self.assertEqual(result['status'], 'failed')
                self.assertFalse(result['quality_verified'])
            finally:
                console.close()

    def test_litecua_agent_hub_telemetry_and_knowledge_remove(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            try:
                apps = console.agent_hub.discover()
                self.assertGreaterEqual(len(apps), 4)
                self.assertIn('aios/litecua_operator', {app['app_id'] for app in apps})

                telemetry = console.telemetry.sample()
                self.assertIn('host', telemetry)

                console.run_litecua({'action': 'observe'})
                console.run_litecua({'action': 'click_search'})
                typed = console.run_litecua({'action': 'type_query', 'text': 'hello-cua'})
                search_el = next(el for el in typed['state']['a11y_tree'] if el['element_id'] == 'input_search_bar')
                self.assertEqual(search_el['value'], 'hello-cua')
                console.run_litecua({'action': 'hotkey_run'})
                restored = console.run_litecua({'action': 'restore_boot'})
                search_el = next(el for el in restored['state']['a11y_tree'] if el['element_id'] == 'input_search_bar')
                self.assertEqual(search_el['value'], '')
                with self.assertRaises(ValueError):
                    console.run_litecua({'action': 'invalid_op'})

                saved = console.knowledge.save({'title': 'temp-doc', 'body': 'searchable knowledge body'})
                self.assertTrue(any(doc['id'] == saved['id'] for doc in console.knowledge.listing()['documents']))
                removed = console.knowledge.remove({'id': saved['id']})
                self.assertTrue(removed['removed'])
                self.assertFalse(any(doc['id'] == saved['id'] for doc in console.knowledge.listing()['documents']))
            finally:
                console.close()
