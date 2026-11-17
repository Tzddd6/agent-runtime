import threading,time,unittest
from unittest.mock import patch
from agent_runtime.model_broker import ModelBroker,BrokerClient,make_server,FEDERATED_BROKER_URL
TOKEN='synthetic-longwait-control-token'
class WaitTests(unittest.TestCase):
 def test_wait_is_woken_by_completion_without_resubmission(self):
  gate=threading.Event()
  class Client:
   def complete(self,*args):gate.wait(2);return {'content':'fixture','finish_reason':'stop'}
  broker=ModelBroker(TOKEN,client_factory=lambda key:Client());broker.configure('synthetic-supplier-fixture')
  call=broker.submit({'request_id':'once','messages':[{'role':'user','content':'fixture'}],'max_tokens':10});result=[]
  thread=threading.Thread(target=lambda:result.append(broker.detail(call['call_id'],.5)));thread.start();time.sleep(.05);gate.set();thread.join(timeout=1)
  self.assertFalse(thread.is_alive());self.assertEqual(result[0]['status'],'succeeded');self.assertEqual(len(broker.receipts),1);broker.close()
 def test_live_http_wait_and_bounds(self):
  class Client:
   def complete(self,*args):time.sleep(.12);return {'content':'fixture','finish_reason':'stop'}
  b=ModelBroker(TOKEN,client_factory=lambda key:Client());b.configure('synthetic-supplier-fixture');server=make_server(b,0);t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
  c=BrokerClient(TOKEN,'http://127.0.0.1:'+str(server.server_port),local_test=True)
  try:
   accepted=c._request('/calls',{'request_id':'once','messages':[{'role':'user','content':'fixture'}],'max_tokens':10});start=time.monotonic();r=c._request('/calls/'+accepted['call_id']+'?wait=0.5');elapsed=time.monotonic()-start
   self.assertEqual(r['status'],'succeeded');self.assertGreater(elapsed,.06);self.assertLess(elapsed,.5)
   with self.assertRaises(ValueError):b.detail(accepted['call_id'],.9)
  finally:b.close();server.shutdown();server.server_close();t.join()
 def test_new_client_uses_wait_only_for_new_protocol_port(self):
  c=BrokerClient(TOKEN,FEDERATED_BROKER_URL);c.profile_id='fixture';paths=[]
  def request(path,body=None):
   paths.append(path)
   if path=='/calls':return {'call_id':'a'*32}
   return {'status':'succeeded','result':{'content':'fixture'},'created':time.time(), 'finished':time.time()}
  with patch.object(c,'_request',side_effect=request):c.complete([{'role':'user','content':'fixture'}],10)
  self.assertEqual(paths,['/calls','/calls/'+'a'*32+'?wait=0.5'])
if __name__=='__main__':unittest.main()
