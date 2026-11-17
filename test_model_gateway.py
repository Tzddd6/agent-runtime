import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from agent_runtime.model_gateway import DeepSeekClient, ModelGateway, ModelError
from agent_runtime.runtime import run


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        owner = self
        self.calls, self.active, self.peak = [], 0, 0
        self.response_content='mock answer'
        self.lock = threading.Lock()

        class Mock(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with owner.lock:
                    owner.active+=1
                    owner.peak=max(owner.peak,owner.active)
                    owner.calls.append(data)
                time.sleep(0.02)
                with owner.lock: owner.active-=1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'model':'deepseek-flash','choices':[{'message':{'content':owner.response_content},'finish_reason':'stop'}],
                                           'usage':{'prompt_tokens':3,'completion_tokens':2}}).encode())

        self.mock=ThreadingHTTPServer(('127.0.0.1',0),Mock)
        self.thread=threading.Thread(target=self.mock.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.mock.server_close)
        self.addCleanup(self.mock.shutdown)
        self.client=DeepSeekClient('test-secret',base=f'http://127.0.0.1:{self.mock.server_port}',local_test=True)

    def test_client_contract_and_validation(self):
        value=self.client.complete([{'role':'user','content':'hello'}])
        self.assertEqual(value['content'],'mock answer')
        self.assertEqual(self.calls[0]['model'],'deepseek-flash')
        self.assertEqual(self.calls[0]['thinking'],{'type':'disabled'})
        with self.assertRaises(ModelError):self.client.complete([{'role':'invalid','content':'hello'}])
        with self.assertRaises(ModelError):DeepSeekClient('key',base='https://example.com')

    def test_empty_provider_content_is_not_success(self):
        self.response_content=''
        with self.assertRaisesRegex(ModelError,'未返回正文'):
            self.client.complete([{'role':'user','content':'hello'}])

    def test_gateway_concurrency_authorization_and_logs(self):
        log=Path(self.temp.name)/'calls.jsonl'
        gateway=ModelGateway(self.client,log,concurrency=1)
        try:
            def request(token):
                req=Request(gateway.url,data=json.dumps({'messages':[{'role':'user','content':'private prompt'}]}).encode(),
                    headers={'Authorization':'Bearer '+token})
                return json.load(urlopen(req,timeout=5))
            with self.assertRaises(HTTPError) as denied:request('wrong')
            self.assertEqual(denied.exception.code,403)
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(3) as pool:
                results=list(pool.map(request,[gateway.token]*3))
            self.assertEqual(len(results),3)
            self.assertEqual(self.peak,1)
            text=log.read_text()
            for secret in ('test-secret','private prompt',gateway.token):self.assertNotIn(secret,text)
        finally:gateway.close()

    def test_worker_llm_integration(self):
        log=Path(self.temp.name)/'calls.jsonl'
        gateway=ModelGateway(self.client,log)
        try:
            with patch('agent_runtime.runtime.ModelGateway.from_env',return_value=gateway):
                with contextlib.redirect_stdout(io.StringIO()):
                    states,events=run([{'id':'llm','action':'llm','messages':[{'role':'user','content':'hello'}]}],output_dir=self.temp.name)
            self.assertEqual(states['llm'],'succeeded')
            self.assertEqual(events[-1]['result']['result'],'mock answer')
            self.assertEqual(events[-1]['result']['model_call']['usage']['prompt_tokens'],3)
        finally:
            # Runtime 已关闭网关，shutdown 可重复调用。
            gateway.close()

    def test_missing_key_fails_without_blocking_independent_task(self):
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':''}),contextlib.redirect_stdout(io.StringIO()):
            states,_=run([{'id':'llm','action':'llm'},{'id':'independent'}],output_dir=self.temp.name)
        self.assertEqual(states,{'llm':'failed','independent':'succeeded'})

    def test_api_key_is_not_in_worker_environment(self):
        import sys
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'must-not-leak'}),contextlib.redirect_stdout(io.StringIO()):
            states,_=run([{'id':'env','action':'command',
                'argv':[sys.executable,'-c',"import os; assert 'DEEPSEEK_API_KEY' not in os.environ"]}],output_dir=self.temp.name)
        self.assertEqual(states['env'],'succeeded')


if __name__=='__main__':unittest.main()
