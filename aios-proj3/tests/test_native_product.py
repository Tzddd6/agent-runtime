import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from cerebrum.llm.apis import LLMResponse
from cerebrum.tool.apis import ToolQuery
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole


class NativeProductTest(unittest.TestCase):
    def test_worker_receipt_and_bad_patch_do_not_break_next_call(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            bad = ToolQuery(tool_calls=[{'name': 'project.patch_check', 'parameters': {'project': 'pricing', 'source': 'import os'}}])
            self.assertGreaterEqual(kernel.execute_request('worker-agent', bad)['response'].status_code, 400)
            good = ToolQuery(tool_calls=[{'name': 'math.calculate', 'parameters': {'expression': '6*7'}}])
            self.assertEqual(kernel.execute_request('worker-agent', good)['response'].response_message, {'result': 42})
            receipts = kernel.process_service.snapshot()['receipts']
            self.assertEqual(len(receipts), 2)
            self.assertTrue(all(row['reclaimed'] and row['pid'] for row in receipts))
            self.assertEqual(kernel.scheduler.drf_controller.used.cpu_milli, 0)

    def test_ram_broker_profiles_and_mock_transport(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            try:
                broker = console.model_service
                secret = 'synthetic-only-private-key'
                broker.request('save', {'id': 'fixture', 'provider': 'openai', 'model': 'fixture', 'label': 'Fixture', 'key': secret})
                self.assertNotIn(secret, json.dumps(broker.request('profiles')))
                self.assertNotEqual(broker.process.pid, __import__('os').getpid())
                from cerebrum.llm.apis import LLMQuery
                response = broker.complete(LLMQuery(model='mock-aios-core', messages=[{'role': 'user', 'content': 'fixture'}]))
                self.assertIn('fixture', response.response_message)
            finally:
                console.close()

    def test_chat_stream_attachment_idempotency_and_native_accounting(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            key = uuid4().hex
            try:
                attachment = console.attachments.upload({'conversation_id': key, 'name': 'fixture.txt',
                                                        'data': base64.b64encode(b'fixture context').decode()})
                events = []
                def complete(query):
                    self.assertIn('fixture context', json.dumps(query.messages))
                    observer = getattr(query, 'stream_observer', None)
                    if observer:
                        observer({'text': '**bold**', 'delta': '**bold**'})
                    return LLMResponse(response_message='**bold**\n\n|a|b|\n|-|-|\n|1|2|', usage={'total_tokens': 12})
                value = {'conversation_id': key, 'request_id': uuid4().hex, 'prompt': 'summarize',
                         'attachments': [attachment['id']], 'mode': 'auto'}
                with patch.object(console.model_service, 'complete', side_effect=complete) as model:
                    first = console.conversations.submit(value, observer=events.append)
                    second = console.conversations.submit(value)
                    self.assertEqual(model.call_count, 1)
                self.assertEqual(first['status'], 'succeeded')
                self.assertEqual(first['answer'], second['answer'])
                self.assertTrue(events)
                self.assertEqual(kernel.syscall_executor.get_history()[-1]['target'], 'llm')
            finally:
                console.close()

    def test_engineering_uses_candidate_and_real_isolated_verification(self):
        from aios.tool.engineering import manifest
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            try:
                def candidate(query):
                    self.assertNotIn('reference_fix', json.dumps(query.messages))
                    return LLMResponse(response_message=json.dumps({'source': manifest('pricing')['reference_fix']}))
                with patch.object(console.model_service, 'complete', side_effect=candidate):
                    turn = console.conversations.submit({'conversation_id': uuid4().hex, 'request_id': uuid4().hex,
                                                         'prompt': 'repair pricing', 'mode': 'engineering'})
                self.assertEqual(turn['status'], 'succeeded')
                verified = json.loads(turn['answer'])
                self.assertTrue(verified['success'])
                self.assertEqual(len(kernel.process_service.snapshot()['receipts']), 2)
            finally:
                console.close()
