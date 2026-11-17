import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request
from agent_runtime.model_gateway import DeepSeekClient,ModelError,ModelGateway


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.entered=threading.Event()
        entered=self.entered
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                raw=json.dumps({'choices':[{'message':{'content':'x'*200}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                entered.set()
                try:
                    for offset in range(0,len(raw),2):
                        self.wfile.write(raw[offset:offset+2]);self.wfile.flush();time.sleep(.02)
                except OSError:pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def test_drip_response_cannot_extend_absolute_deadline(self):
        client=DeepSeekClient('fixture-key',base=self.base,local_test=True,timeout=.7)
        start=time.monotonic()
        with self.assertRaisesRegex(ModelError,'绝对总期限'):
            client.complete([{'role':'user','content':'fixture'}])
        self.assertTrue(self.entered.is_set())
        self.assertLess(time.monotonic()-start,1.8)

    def test_gateway_close_cancels_local_broker_process(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway=ModelGateway(DeepSeekClient('fixture-key',base=self.base,local_test=True,timeout=10),
                                 Path(directory)/'calls.jsonl')
            def call():
                try:
                    request=urllib.request.Request(gateway.url,
                        data=json.dumps({'messages':[{'role':'user','content':'fixture'}]}).encode(),
                        headers={'Authorization':'Bearer '+gateway.token})
                    with urllib.request.urlopen(request,timeout=15) as response:response.read()
                except (OSError,ValueError):pass
            worker=threading.Thread(target=call,daemon=True)
            worker.start()
            self.assertTrue(self.entered.wait(3))
            start=time.monotonic()
            gateway.close()
            worker.join(2)
            self.assertLess(time.monotonic()-start,2)
            self.assertFalse(worker.is_alive())


if __name__=='__main__':unittest.main()
