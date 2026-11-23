import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole, create_server


class NativeWebTest(unittest.TestCase):
    def test_real_native_job_and_security(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            with create_server(console, 0) as server:
                thread = Thread(target=server.serve_forever)
                thread.start()
                base = f'http://127.0.0.1:{server.server_port}'
                opener = build_opener(ProxyHandler({}))

                def get(path):
                    with opener.open(base + path, timeout=5) as response:
                        return json.load(response)

                try:
                    csrf = get('/api/status')['csrf']
                    with self.assertRaises(HTTPError) as blocked:
                        opener.open(Request(base + '/api/jobs', data=b'{"preset":"document"}'))
                    self.assertEqual(blocked.exception.code, 403)
                    blocked.exception.close()
                    payload = {'preset': 'document', 'prompt': 'hello native kernel'}
                    request = Request(base + '/api/jobs', data=json.dumps(payload).encode(), headers={'X-AgentRT-CSRF': csrf})
                    with opener.open(request, timeout=5) as response:
                        identity = json.load(response)['id']
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline:
                        job = get('/api/jobs/' + identity)
                        if job['status'] != 'running':
                            break
                        time.sleep(0.01)
                    self.assertEqual(job['status'], 'succeeded')
                    self.assertEqual(job['states']['stats'], 'succeeded')
                    self.assertEqual(kernel.syscall_executor.get_history()[-1]['target'], 'tool')
                    restored = NativeConsole(kernel, Path(folder) / 'jobs')
                    try:
                        self.assertEqual(restored.detail(identity)['status'], 'succeeded')
                    finally:
                        restored.close()
                finally:
                    server.shutdown()
                    thread.join()
                    console.close()
