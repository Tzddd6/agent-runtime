import json,tempfile,threading,time,unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from agent_runtime.model_gateway import DeepSeekClient,ModelGateway


class AgentModelCancelTests(unittest.TestCase):
    def test_revoking_one_attempt_cancels_only_its_local_provider_child(self):
        started=threading.Event();release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if value['messages'][0]['content']=='slow':started.set();release.wait(3)
                raw=json.dumps({'choices':[{'message':{'content':'OK'}}],'usage':{}}).encode()
                try:
                    self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                except OSError:pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.daemon_threads=True
        threading.Thread(target=server.serve_forever,daemon=True).start()
        with tempfile.TemporaryDirectory() as directory:
            client=DeepSeekClient('offline-only-fixture-key',base='http://127.0.0.1:'+str(server.server_port),local_test=True,timeout=4)
            gateway=ModelGateway(client,Path(directory)/'calls.jsonl')
            first,second=gateway.issue('alpha'),gateway.issue('beta')
            def call(token,text):
                request=Request(gateway.url,data=json.dumps({'messages':[{'role':'user','content':text}]}).encode(),headers={'Authorization':'Bearer '+token})
                try:
                    with urlopen(request,timeout=5) as response:return response.status,json.load(response)
                except HTTPError as e:return e.code,{}
            try:
                with ThreadPoolExecutor(2) as pool:
                    slow=pool.submit(call,first,'slow');self.assertTrue(started.wait(3))
                    mark=time.monotonic();gateway.revoke(first)
                    fast=pool.submit(call,second,'fast')
                    self.assertEqual(slow.result(2)[0],502)
                    self.assertLess(time.monotonic()-mark,2)
                    self.assertEqual(fast.result(3)[1]['content'],'OK')
                self.assertEqual(call(first,'new')[0],403)
            finally:release.set();gateway.close();server.shutdown();server.server_close()
