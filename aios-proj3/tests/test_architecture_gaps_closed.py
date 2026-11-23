"""Tests verifying closure of the 4 remaining proj3 architecture gaps."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from aios.llm_core.model_service import ModelService
from aios.tool.engineering import manifest
from cerebrum.llm.apis import LLMQuery, LLMResponse
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole


class ArchitectureGapsClosedTest(unittest.TestCase):
    def test_concurrent_model_service_multiplexes_requests(self):
        ms = ModelService()
        try:
            def call_one(i):
                return ms.complete(LLMQuery(model='mock-aios-core', messages=[{'role': 'user', 'content': f'msg-{i}'}]))
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(call_one, range(6)))
            self.assertEqual(len(results), 6)
            for i, resp in enumerate(results):
                self.assertEqual(resp.status_code, 200)
                self.assertIn(f'msg-{i}', resp.response_message)
        finally:
            ms.close()

    def test_resident_worker_supervisor_socket_heartbeat_and_crash_recovery(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store'), log_mode='silent') as kernel:
            sup = kernel.worker_supervisor
            out1 = sup.execute('agent_w1', 'math.calculate', {'expression': '(10+5)*2'})
            self.assertEqual(out1['result'], 30)
            hb = sup.check_health('agent_w1')
            self.assertTrue(hb['ok'])
            pid1 = hb['pid']
            self.assertIsNotNone(pid1)

            # Simulate unexpected worker crash (SIGKILL / terminate)
            worker = sup.ensure_worker('agent_w1')
            worker.proc.kill()
            worker.proc.wait(timeout=3)

            # Heartbeat detects crash and auto-restarts with a fresh PID
            hb2 = sup.check_health('agent_w1')
            self.assertTrue(hb2.get('restarted'))
            self.assertNotEqual(hb2['pid'], pid1)
            self.assertEqual(sup.snapshot()['agent_w1']['restarts'], 1)

            # Subsequent task succeeds on the restarted resident worker
            out2 = sup.execute('agent_w1', 'text.stats', {'text': 'hello resident worker'})
            self.assertEqual(out2['words'], 3)

    def test_engineering_dynamic_round_2_spawn_repairs_failed_first_patch(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store'), log_mode='silent') as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            try:
                spec = manifest('pricing')
                calls = []
                def two_round_model(query):
                    calls.append(query.messages)
                    if len(calls) == 1:
                        # Round 1 returns original buggy source -> fails isolated verification
                        return LLMResponse(response_message=json.dumps({'source': spec['source']}))
                    # Round 2 receives failure feedback and returns fixed source
                    self.assertIn('verification_feedback', json.dumps(query.messages, ensure_ascii=False))
                    return LLMResponse(response_message=json.dumps({'source': spec['reference_fix']}))

                with patch.object(console.model_service, 'complete', side_effect=two_round_model):
                    turn = console.conversations.submit({
                        'conversation_id': uuid4().hex,
                        'request_id': uuid4().hex,
                        'prompt': 'repair pricing',
                        'mode': 'engineering',
                        'project': 'pricing',
                    })
                self.assertEqual(len(calls), 2)
                self.assertEqual(turn['status'], 'succeeded')
                verified = json.loads(turn['answer'])
                self.assertTrue(verified['success'])
                self.assertTrue(verified['artifact']['readback_verified'])
            finally:
                console.close()
