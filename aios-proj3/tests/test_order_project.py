import io
import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread, Event
import time
import unittest
from urllib.request import build_opener, ProxyHandler
from urllib.error import HTTPError
from uuid import uuid4
from zipfile import ZipFile

from aios.tool import order_project as order
from cerebrum.llm.apis import LLMResponse
from cerebrum.tool.apis import ToolQuery
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole, create_server


def fixed_files():
    files = dict(order.SOURCE)
    files['order_app/pricing.py'] = files['order_app/pricing.py'].replace(
        'unit_cents * quantity - discount_pct', 'unit_cents * quantity * (100 - discount_pct) // 100')
    files['order_app/shipping.py'] = files['order_app/shipping.py'].replace('> 5000', '>= 5000')
    files['order_app/orders.py'] = files['order_app/orders.py'].replace('return subtotal', 'return subtotal + shipping_cents(subtotal)')
    return files


class OrderProjectAcceptance(unittest.TestCase):
    def test_json_fence_is_local_normalization_not_another_model_call(self):
        from runtime.engineering_workflow import decode_patch
        self.assertEqual(decode_patch('```json\n'+json.dumps({'files':fixed_files()})+'\n```'),fixed_files())
        self.assertEqual(decode_patch('Explanation only.\n```json\n'+json.dumps({'files':fixed_files()})+'\n```'),fixed_files())
        with self.assertRaises(ValueError): decode_patch(json.dumps({'files':fixed_files()})*2)
        from runtime.engineering_workflow import patch_messages
        forward=fixed_files(); reverse=dict(reversed(list(forward.items())))
        self.assertEqual(patch_messages(forward,'same feedback'),patch_messages(reverse,'same feedback'))
        with self.assertRaises(ValueError): decode_patch('```json\n{}')
    def test_console_uses_cgroup_manager_public_root_attribute(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            kernel.cgroup_manager.enabled = True
            kernel.cgroup_manager.cgroup_root = folder
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            console.close()

    def test_path_import_and_test_tampering_rejected(self):
        for path, text in [('tests/test_orders.py', 'pass'), ('../escape.py', 'pass')]:
            with self.assertRaises(ValueError): order.validate_files({**fixed_files(), path: text})
        files = fixed_files()
        files['order_app/orders.py'] = 'import os\n' + files['order_app/orders.py']
        with self.assertRaises(ValueError): order.validate_files(files)
        files = fixed_files()
        files['order_app/orders.py'] = files['order_app/orders.py'].replace('return subtotal + shipping_cents(subtotal)', "return open('secret').read()")
        with self.assertRaises(ValueError): order.validate_files(files)

    def test_zero_skipped_and_fake_success_rejected(self):
        good = {'tests_run': 13, 'failures': 0, 'errors': 0, 'skipped': 0, 'success': True}
        self.assertTrue(order.check_report(good, 0))
        for field, value in [('tests_run', 0), ('skipped', 13), ('errors', 1), ('success', False)]:
            self.assertFalse(order.check_report({**good, field: value}, 0))
        self.assertFalse(order.check_report(good, 1))

    def test_real_worker_baseline_and_unittest(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=folder) as kernel:
            def check(files):
                r = kernel.execute_request('test-order', ToolQuery(tool_calls=[
                    {'name': 'project.workspace_check', 'parameters': {'files': files}}]))['response']
                self.assertEqual(r.status_code, 200, r.error)
                return r.response_message
            baseline = check(dict(order.SOURCE))
            self.assertFalse(baseline['success'])
            self.assertGreater(baseline['failures'], 0)
            result = check(fixed_files())
            self.assertTrue(result['success'])
            self.assertEqual(result['tests_run'], 13)
            self.assertEqual(result['test_engine'], 'python_unittest_subprocess')
            self.assertEqual(kernel.process_service.snapshot()['active'], 0)
            snapshot=kernel.process_service.snapshot()
            snapshot['receipts'][-1]['limits']['cpu_milli']=999999
            self.assertNotEqual(kernel.process_service.snapshot()['receipts'][-1]['limits']['cpu_milli'],999999)

    def test_two_round_workflow_download_integrity_and_dedup(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            calls = []
            def complete(query):
                calls.append(query.messages)
                files = dict(order.SOURCE) if len(calls) == 1 else fixed_files()
                return LLMResponse(response_message=json.dumps({'files': files}))
            console.model_service.complete = complete
            value = {'conversation_id': uuid4().hex, 'request_id': uuid4().hex,
                     'mode': 'engineering', 'project': 'order_project', 'prompt': '修复订单工程'}
            try:
                result = console.conversations.submit(value)
                self.assertEqual(result['status'], 'succeeded')
                output = json.loads(result['answer'])
                self.assertEqual(len(set(output['agents'].values())), 5)
                self.assertEqual(output['tests_run'], 13)
                self.assertNotIn('reference_fix', json.dumps(calls))
                self.assertNotIn('class OrderTests', json.dumps(calls))
                duplicate = console.conversations.submit(value)
                self.assertEqual(duplicate['answer'], result['answer'])
                self.assertEqual(len(calls), 2)
                with self.assertRaises(ValueError):
                    console.conversations.submit({**value, 'project': 'pricing'})
                self.assertEqual(console.conversations.store.read(value['conversation_id'])['turns'][0]['status'], 'succeeded')
                for name in output['agents'].values():
                    self.assertEqual(kernel.scheduler.drf_controller.get_or_create_agent(name).state, 'terminated')
                self.assertEqual(kernel.process_service.snapshot()['active'], 0)
                job = console.detail(result['job_id'])
                self.assertEqual(job['states']['verify_round_2'], 'succeeded')
                with create_server(console, 0) as server:
                    thread = Thread(target=server.serve_forever);thread.start()
                    opener = build_opener(ProxyHandler({}))
                    url = f"http://127.0.0.1:{server.server_port}" + output['artifact']['download_url']
                    try:
                        with opener.open(url) as response: raw = response.read()
                        with ZipFile(io.BytesIO(raw)) as archive:
                            delivered = {p: archive.read(p).decode() for p in order.SOURCE}
                        self.assertEqual(order.digest(delivered), output['source_sha256'])
                        (console.root / 'artifacts' / (result['job_id'] + '.zip')).write_bytes(b'tampered')
                        with self.assertRaises(HTTPError) as error: opener.open(url)
                        self.assertEqual(error.exception.code, 404)
                        error.exception.close()
                    finally: server.shutdown();thread.join()
            finally: console.close()

    def test_budget_prevents_provider_and_releases_roles(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            calls = []
            def complete(query):
                calls.append(query)
                raise AssertionError('Zero budget must not contact provider')
            console.model_service.complete = complete
            try:
                result = console.conversations.submit({'conversation_id': uuid4().hex, 'request_id': uuid4().hex,
                    'mode': 'engineering', 'project': 'order_project', 'prompt': 'repair',
                    'model_budget': {'calls': 0, 'output_tokens': 0}})
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(calls, [])
                self.assertEqual(kernel.process_service.snapshot()['active'], 0)
                roles = [a for n, a in kernel.scheduler.drf_controller.agents.items() if n.startswith('order-')]
                self.assertEqual(len(roles), 5)
                self.assertTrue(all(a.state == 'terminated' for a in roles))
            finally: console.close()

    def test_failed_second_patch_does_not_publish_artifact(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            calls = []
            def complete(query):
                calls.append(1)
                return LLMResponse(response_message=json.dumps({'files': order.SOURCE}))
            console.model_service.complete = complete
            try:
                result = console.conversations.submit({'conversation_id': uuid4().hex, 'request_id': uuid4().hex,
                    'mode': 'engineering', 'project': 'order_project', 'prompt': 'repair'})
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(len(calls), 2)
                self.assertFalse(list(console.root.glob('artifacts/*.zip')))
            finally: console.close()

    def test_provider_timeout_is_terminal_and_next_request_recovers(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs');calls=[]
            def broken(query):
                calls.append(1)
                raise TimeoutError('synthetic uncertain provider timeout')
            console.model_service.complete=broken
            try:
                failed=console.conversations.submit({'conversation_id':uuid4().hex,'request_id':uuid4().hex,
                    'mode':'engineering','project':'order_project','prompt':'repair'})
                self.assertEqual(failed['status'],'failed');self.assertEqual(len(calls),1)
                self.assertEqual(kernel.process_service.snapshot()['active'],0)
                console.model_service.complete=lambda query: LLMResponse(response_message=json.dumps({'files':fixed_files()}))
                recovered=console.conversations.submit({'conversation_id':uuid4().hex,'request_id':uuid4().hex,
                    'mode':'engineering','project':'order_project','prompt':'repair'})
                self.assertEqual(recovered['status'],'succeeded')
            finally:console.close()

    def test_stream_disconnect_keeps_result_and_replay_does_not_call_provider(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console=NativeConsole(kernel,Path(folder)/'jobs');calls=[];started=Event();release=Event()
            def complete(query):
                calls.append(1);query.stream_observer({'text':'partial','delta':'partial'});started.set()
                release.wait(2)
                return LLMResponse(response_message='complete persisted result')
            console.model_service.complete=complete
            value={'conversation_id':uuid4().hex,'request_id':uuid4().hex,'mode':'llm','prompt':'disconnect fixture'}
            try:
                with create_server(console,0) as server:
                    thread=Thread(target=server.serve_forever);thread.start()
                    connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                    try:
                        connection.request('POST','/api/chat/turns',json.dumps({**value,'stream':True}),
                                           {'Content-Type':'application/json','X-AgentRT-CSRF':console.csrf})
                        response=connection.getresponse();self.assertEqual(response.status,200)
                        self.assertTrue(started.wait(2));response.read(1);connection.close();release.set()
                        deadline=time.monotonic()+4
                        while console.conversations.store.read(value['conversation_id'])['turns'][0]['status']=='running':
                            if time.monotonic()>deadline:self.fail('Disconnected request did not settle')
                            time.sleep(.01)
                        result=console.conversations.submit(value)
                        self.assertEqual(result['status'],'succeeded');self.assertEqual(len(calls),1)
                        self.assertEqual(result['answer'],'complete persisted result')
                    finally:release.set();connection.close();server.shutdown();thread.join()
            finally:console.close()
