import json,os,tempfile,threading,time,unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from agent_runtime.model_gateway import DeepSeekClient,ModelGateway,ModelError
from agent_runtime.provider_stream import extract,PREFIX
from agent_runtime.routing import auto_tasks
from agent_runtime.runtime import run

class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.requests=[];self.missing_done=False;self.multiline=False;owner=self
        class Provider(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                owner.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                def send(data):
                    if owner.multiline:
                        wire=': keepalive\r\nevent: message\r\n'+''.join('data: '+line+'\r\n' for line in json.dumps(data,ensure_ascii=False,indent=2).splitlines())+'\r\n'
                        self.wfile.write(wire.encode())
                    else:self.wfile.write(b'data: '+json.dumps(data,ensure_ascii=False).encode()+b'\n\n')
                    self.wfile.flush()
                try:
                    send({'choices':[{'delta':{'reasoning_content':'PRIVATE-REASONING'}}]})
                    send({'choices':[{'delta':{'content':'第一段：**正文**。\n'}}]});time.sleep(.25)
                    send({'choices':[{'delta':{'content':'第二段完成。'},'finish_reason':'stop'}],'usage':{'prompt_tokens':31,'completion_tokens':17}})
                    if not owner.missing_done:self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
                except OSError:pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Provider);threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.client=DeepSeekClient('offline-stream-key',base='http://127.0.0.1:'+str(self.server.server_port),local_test=True)
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
    def gateway(self):
        g=ModelGateway(self.client,Path(self.directory.name)/'calls.jsonl');self.addCleanup(g.close);return g
    def request(self,g,token):
        req=Request(g.url,data=json.dumps({'messages':[{'role':'user','content':'解释示例'}],'max_tokens':256}).encode(),headers={'Authorization':'Bearer '+token})
        return json.load(urlopen(req,timeout=6))
    def test_true_deltas_arrive_before_complete_and_reasoning_and_keys_never_emit(self):
        events=[];g=self.gateway();g.output_observer=lambda e:events.append((time.monotonic(),e))
        token=g.issue('assistant',stream_key='auto_answer');result=self.request(g,token);finished=time.monotonic()
        self.assertGreaterEqual(len(events),2);self.assertLess(events[0][0],finished-.15)
        self.assertEqual(result['content'],'第一段：**正文**。\n第二段完成。');self.assertEqual(result['stream_deltas'],len(events))
        self.assertEqual(result['usage']['completion_tokens'],17);self.assertEqual(len(self.requests),1)
        self.assertTrue(self.requests[0]['stream']);self.assertNotIn(PREFIX,str(self.requests));self.assertFalse(g.stream_tokens)
        for hidden in ('PRIVATE-REASONING','offline-stream-key',token):self.assertNotIn(hidden,str(events))
        log=(Path(self.directory.name)/'calls.jsonl').read_text()
        for hidden in ('PRIVATE-REASONING','offline-stream-key',token,'第一段'):self.assertNotIn(hidden,log)
    def test_worker_receives_live_observation_and_preserves_final_accounting(self):
        events=[]
        with patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda path:ModelGateway(self.client,path)):
            states,records=run(auto_tasks('解释示例'),quiet=True,output_dir=Path(self.directory.name)/'runs',cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'),output_observer=events.append,model_budget={'calls':1,'output_tokens':4096})
        self.assertEqual(states,{'auto_answer':'succeeded'});self.assertTrue(events);self.assertEqual(events[0]['source'],'provider_content_delta')
        result=next(e['result'] for e in records if e['state']=='succeeded');self.assertEqual(result['model_call']['provider_calls'],1)
    def test_chat_completions_accepts_multiline_sse_without_replaying(self):
        self.multiline=True;events=[];g=self.gateway();g.output_observer=events.append
        result=self.request(g,g.issue('assistant',stream_key='auto_answer'))
        self.assertEqual(result['content'],'第一段：**正文**。\n第二段完成。')
        self.assertEqual(len(self.requests),1);self.assertGreaterEqual(len(events),2)
    def test_unfinished_sse_fails_without_replaying_and_callback_cannot_be_forged(self):
        self.missing_done=True;g=self.gateway();g.output_observer=lambda e:None;token=g.issue('a',stream_key='auto_answer')
        with self.assertRaises(HTTPError):self.request(g,token)
        self.assertEqual(len(self.requests),1);self.assertFalse(g.stream_tokens)
        req=Request(g.url.removesuffix('/complete')+'/progress',data=b'{"delta":"forged"}',headers={'Authorization':'Bearer '+token})
        with self.assertRaises(HTTPError) as error:urlopen(req,timeout=3)
        self.assertEqual(error.exception.code,403)
    def test_callback_only_accepts_ephemeral_loopback_and_marker_is_not_a_user_tool(self):
        for url in ['https://example.com/progress','http://127.0.0.1:88/private','file:///etc/shadow','http://user@127.0.0.1:88/progress']:
            with self.subTest(url=url),self.assertRaises((ModelError,ValueError)):
                extract([{'role':'system','content':PREFIX+json.dumps({'url':url,'token':'x'*40})}])
        g=self.gateway();token=g.issue('a')
        req=Request(g.url,data=json.dumps({'messages':[{'role':'system','content':PREFIX+'bad'},{'role':'user','content':'plain'}]}).encode(),headers={'Authorization':'Bearer '+token})
        # With no output subscription, marker injection is stripped. The fixture
        # emits SSE to a non-SSE client, so it fails once rather than calling a URL.
        with self.assertRaises(HTTPError):urlopen(req,timeout=3)
        self.assertNotIn(PREFIX,str(self.requests))
    def test_revoking_during_stream_stops_progress_and_does_not_replay(self):
        from concurrent.futures import ThreadPoolExecutor
        first=threading.Event();events=[];g=self.gateway();g.output_observer=lambda e:(events.append(e),first.set());token=g.issue('a',stream_key='auto_answer')
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(self.request,g,token);self.assertTrue(first.wait(3));g.revoke(token)
            with self.assertRaises(HTTPError):future.result(timeout=5)
        self.assertEqual(len(self.requests),1);self.assertFalse(g.stream_tokens)
    def test_local_formula_assets_keep_auth_and_reject_arbitrary_paths(self):
        from agent_runtime.web import Controller,make_server
        c=Controller(Path(self.directory.name)/'web');server=make_server(c,port=0,proxy_token='protected-test-token')
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            base='http://127.0.0.1:'+str(server.server_port)
            with self.assertRaises(HTTPError) as denied:urlopen(base+'/static/vendor/katex/katex.min.js',timeout=3)
            self.assertEqual(denied.exception.code,403)
            for name in ['katex.min.js','katex.min.css','fonts/KaTeX_Main-Regular.woff2']:
                with urlopen(Request(base+'/static/vendor/katex/'+name,headers={'X-AgentRT-Proxy-Token':'protected-test-token'}),timeout=3) as r:self.assertEqual(r.status,200)
            with self.assertRaises(HTTPError) as blocked:urlopen(Request(base+'/static/vendor/katex/../../model_call.py',headers={'X-AgentRT-Proxy-Token':'protected-test-token'}),timeout=3)
            self.assertEqual(blocked.exception.code,404)
        finally:server.shutdown();server.server_close()
