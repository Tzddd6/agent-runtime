import io,json,os,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from agent_runtime.model_gateway import AizexClient,ModelError
from agent_runtime.model_broker import ModelBroker,BrokerClient,make_server
from agent_runtime.failures import model_code
from agent_runtime.runtime import run
class DeadlineTests(unittest.TestCase):
 def test_structured_timeout_survives_broker_without_private_text(self):
  class Failing:
   def complete(self,*args):raise ModelError('private-sentinel',code='MODEL_TIMEOUT')
  b=ModelBroker('synthetic-control-token',client_factory=lambda key:Failing());b.configure('synthetic-supplier-not-real')
  server=make_server(b,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   c=BrokerClient('synthetic-control-token','http://127.0.0.1:'+str(server.server_port),local_test=True)
   with self.assertRaises(ModelError) as caught:c.complete([{'role':'user','content':'fixture'}],8)
   self.assertEqual(model_code(caught.exception),'MODEL_TIMEOUT');self.assertNotIn('private-sentinel',str(caught.exception));self.assertEqual(len(b.receipts),1)
  finally:b.close();server.shutdown();server.server_close();thread.join()
 def test_parent_deadline_cancels_without_retry_then_new_bounded_call_finishes(self):
  from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
  requests=[]
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_POST(self):
    body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(body)
    time.sleep(.3)
    raw=json.dumps({'model':'fixture','status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'fixture'}]}],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}).encode()
    try:self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    except OSError:pass
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_port)
  try:
   with self.assertRaises(ModelError) as caught:AizexClient('synthetic-key',timeout=.18,base=base,local_test=True).complete([{'role':'user','content':'fixture'}],8)
   self.assertEqual(model_code(caught.exception),'MODEL_TIMEOUT')
   result=AizexClient('synthetic-key',timeout=2,base=base,local_test=True).complete([{'role':'user','content':'fixture'}],8)
   self.assertEqual(result['content'],'fixture');self.assertIn(len(requests),(1,2)) # Very short deadline may expire during child startup; never more than one attempt per call.
  finally:server.shutdown();server.server_close();thread.join()
 def test_explicit_sdk_deadline_remains_and_web_policy_is_opt_in(self):
  class Slow:
   model='fixture';timeout=2
   def complete(self,*args):time.sleep(.3);return {'content':'fixture','finish_reason':'stop','usage':{'prompt_tokens':1,'completion_tokens':1}}
  with tempfile.TemporaryDirectory() as tmp:
   task={'id':'x','action':'llm','messages':[{'role':'user','content':'fixture'}],'timeout':.1}
   states,_=run([task],quiet=True,output_dir=Path(tmp)/'short',model_client=Slow(),cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
   self.assertNotEqual(states['x'],'succeeded')
   states,_=run([task],quiet=True,output_dir=Path(tmp)/'long',model_client=Slow(),model_task_timeout=1,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
   self.assertEqual(states['x'],'succeeded');self.assertEqual(task['timeout'],.1)
if __name__=='__main__':unittest.main()
