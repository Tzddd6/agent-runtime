import json
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from aios.llm_core.diagnostics import MESSAGES, check_model_list, completion_issue
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole


class SubmissionAndDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.kernel = self.enterContext(AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')))
        self.root = Path(folder) / 'jobs'
        self.console = NativeConsole(self.kernel, self.root)
        self.addCleanup(self.console.close)

    def test_rejected_requests_leave_history_intact_across_restart(self):
        history = {f'completed{i}': {'id': f'completed{i}', 'status': 'succeeded'} for i in range(20)}
        for job in history.values():
            self.console.save(job)
        self.console.jobs.update(history)
        with self.assertRaisesRegex(ValueError, '选用模型'):
            self.console.submit({'preset': 'llm'})
        with patch.object(self.console.pool, 'submit', side_effect=RuntimeError('closed')):
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                self.console.submit({'preset': 'kernel'})
        self.assertEqual(self.console.jobs, history)
        self.assertEqual({p.stem for p in self.root.glob('*.json')}, set(history))
        restored = NativeConsole(self.kernel, self.root)
        self.addCleanup(restored.close)
        self.assertEqual(restored.jobs, history)

    def test_legacy_running_job_recovered_on_disk(self):
        self.console.save({'id': 'old', 'status': 'running'})
        restored = NativeConsole(self.kernel, self.root)
        self.addCleanup(restored.close)
        self.assertEqual(restored.detail('old')['status'], 'interrupted')
        self.assertEqual(json.loads((self.root / 'old.json').read_text())['status'], 'interrupted')

    def test_model_service_checks_selected_catalog_and_chat_reports_auth_failure(self):
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path != '/v1/models':
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[{"id":"available-model"}]}')

            def do_POST(self):
                self.send_error(401, 'private-provider-detail')

        server = self.enterContext(ThreadingHTTPServer(('127.0.0.1', 0), Provider))
        thread = Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        service = self.console.model_service
        self.assertEqual(service.request('test_connection')['code'], 'unconfigured')
        profile = {'id': 'local', 'provider': 'vllm', 'label': 'Local', 'key': 'synthetic-profile-value',
                   'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'model': 'wrong-model'}
        service.request('save', profile)
        self.assertEqual(service.request('test_connection', {'id': 'local'})['code'], 'model')
        self.assertEqual(service.request('status')['profile_id'], 'legacy')
        self.assertEqual(service.request('status')['model'], 'deepseek-flash')
        service.request('save-and-select', {k: v for k, v in {**profile, 'model': 'available-model'}.items() if k != 'key'})
        self.assertIsNone(service.request('status')['connection'])
        self.assertTrue(service.request('test_connection')['ok'])
        self.assertEqual(service.request('status')['connection']['code'], 'available')
        self.assertNotIn(profile['key'], json.dumps(service.request('status')))
        turn = self.console.conversations.submit({'conversation_id': 'conv_test', 'request_id': 'req_test',
                                                  'mode': 'llm', 'prompt': 'Test authentication'})
        self.assertEqual(turn['status'], 'failed')
        self.assertEqual(turn['answer'], MESSAGES['auth'])
        self.assertEqual(turn['model_error_code'], 'auth')
        self.assertEqual(turn['model_route']['profile_id'], 'local')
        self.assertEqual(turn['model_route']['model'], 'available-model')
        self.assertNotIn('private-provider-detail', json.dumps(self.console.detail(turn['job_id'])))

    def test_unreachable_provider_returns_network_diagnostic(self):
        config = {'hostname': 'https://localhost', 'api_key': 'fixture', 'name': 'example'}
        with patch('aios.llm_core.diagnostics.urlopen', side_effect=URLError('connection refused')):
            self.assertEqual(check_model_list(config), 'network')

    def test_upstream_model_error_is_sanitized(self):
        body = BytesIO(b'{"error":{"code":"model_not_found","message":"private detail"}}')
        with HTTPError('https://localhost', 400, 'Bad Request', {}, body) as exc:
            self.assertEqual(completion_issue(exc), 'model')
