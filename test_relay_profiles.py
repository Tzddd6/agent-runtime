import json,tempfile,unittest
from unittest.mock import patch
from agent_runtime.model_gateway import AizexClient,OpenAIClient,ModelError,inference_options
from agent_runtime.model_profiles import MultiModelBroker
from agent_runtime.federated_profiles import FederatedProfiles
from agent_runtime.model_broker import BrokerClient,FEDERATED_BROKER_URL,MULTI_BROKER_URL
TOKEN='synthetic-routing-control-not-secret'
KEY='synthetic-rotation-fixture-not-a-key'
class Upstream:
 def __init__(self):self.closed=False;self.busy=0;self.revision=1;self.calls=0
 def status(self):return {'profile_id':'old','busy':self.busy,'configured':True,'model':'gpt-4.1-mini'}
 def profiles_info(self):return {'profiles':[{'id':'legacy','label':'Legacy','provider':'deepseek','model':'deepseek-flash','revision':None,'configured':True,'legacy':True},{'id':'old','label':'Old','provider':'openai','model':'gpt-4.1-mini','revision':self.revision,'configured':True,'legacy':False}]}
 def for_profile(self,identity,model):
  from types import SimpleNamespace
  return SimpleNamespace(profile_revision=self.revision,model=model)
 def close(self):self.closed=True
class RelayTests(unittest.TestCase):
 def test_third_party_origin_is_fixed_and_distinct(self):
  self.assertEqual(AizexClient(KEY).base,'https://ca.memofun.net/v1')
  for base in ('https://api.openai.com/v1','http://127.0.0.1:80','https://codex-api.aizex.net/v1','https://ca.memofun.net.evil/v1'):
   with self.assertRaises(ModelError):AizexClient(KEY,base=base)
  with self.assertRaises(ModelError):OpenAIClient(KEY,base='https://ca.memofun.net/v1')
  self.assertEqual(inference_options(AizexClient(KEY),128),{'max_completion_tokens':128,'store':False,'reasoning_effort':'minimal'})
 def test_real_trusted_child_uses_relay_protocol_and_fixed_origin(self):
  import threading
  from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
  captured=[]
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_POST(self):
    request=json.loads(self.rfile.read(int(self.headers['Content-Length'])));captured.append((self.path,request))
    raw=json.dumps({'model':'gpt-6-luna','status':'completed','output':[{'type':'reasoning','summary':[]},{'type':'message','role':'assistant','content':[{'type':'output_text','text':'{"ready":true}'}]}],'usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15}}).encode()
    self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   client=AizexClient(KEY,base='http://127.0.0.1:'+str(server.server_port),local_test=True)
   result=client.complete([{'role':'user','content':'synthetic fixture'}],128)
   self.assertEqual(json.loads(result['content']),{'ready':True});self.assertEqual(captured[0][0],'/responses')
   body=captured[0][1];self.assertNotIn('thinking',body);self.assertEqual(body['reasoning']['effort'],'minimal');self.assertEqual(body['max_output_tokens'],128)
  finally:server.shutdown();server.server_close();thread.join()
 def test_relay_reasoning_native_protocol_omits_unsupported_sampling(self):
  from agent_runtime.benchmark_transport import complete,encode
  captured=[]
  class Response:
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def read(self,*args):return json.dumps({'model':'gpt-6-luna','status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'fixture'}]}],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}).encode()
  class Opener:
   def open(self,request,**kwargs):captured.append(json.loads(request.data));return Response()
  with patch('agent_runtime.model_network.provider_opener',return_value=Opener()):
   complete(AizexClient(KEY),encode([{'role':'user','content':'fixture'}],temperature=0,seed=1),128)
  self.assertNotIn('temperature',captured[0]);self.assertNotIn('seed',captured[0]);self.assertEqual(captured[0]['reasoning']['effort'],'minimal')
 def test_keys_cannot_change_origin_by_metadata_edit(self):
  b=MultiModelBroker(TOKEN);b.save_profile({'id':'p','label':'test','provider':'openai','model':'gpt-4.1-mini','key':KEY})
  with self.assertRaises(ValueError):b.save_profile({'id':'p','label':'test','provider':'aizex','model':'gpt-6-luna'})
  b.close()
 def test_upgrade_preserves_upstream_and_no_key_is_copied(self):
  upstream=Upstream();b=FederatedProfiles(TOKEN,upstream=upstream,legacy_client=upstream)
  self.assertEqual(b.selected,'old');self.assertEqual(b.profiles,{})
  b.save_profile({'id':'new','label':'relay','provider':'aizex','model':'gpt-6-luna','key':KEY})
  self.assertEqual(b.selected,'old');self.assertNotIn(KEY,json.dumps(b.profiles_info()))
  b.select_profile({'id':'new'});self.assertEqual(b.status()['provider'],'aizex')
  b.close();self.assertFalse(upstream.closed)
 def test_revision_and_busy_are_enforced(self):
  upstream=Upstream();b=FederatedProfiles(TOKEN,upstream=upstream,legacy_client=upstream)
  with self.assertRaises(ModelError):b.snapshot_client({'profile_id':'old','profile_revision':2})
  factory,metadata=b.snapshot_client({'profile_id':'old','profile_revision':1});self.assertEqual(factory().profile_revision,1)
  upstream.busy=1
  with self.assertRaises(ModelError):b.select_profile({'id':'old'})
  b.close()
 @unittest.skipUnless(__import__('os').name=='posix','private metadata uses Unix owner permissions')
 def test_restart_does_not_fall_back_from_lost_local_key(self):
  from pathlib import Path
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'selection.json';upstream=Upstream();b=FederatedProfiles(TOKEN,upstream=upstream,legacy_client=upstream,selection_path=path)
   b.save_profile({'id':'local','label':'relay','provider':'aizex','model':'gpt-6-luna','key':KEY});b.select_profile({'id':'local'});b.close()
   self.assertEqual(json.loads(path.read_text()),{'selected':'local'});self.assertNotIn(KEY,path.read_text())
   after=FederatedProfiles(TOKEN,upstream=upstream,legacy_client=upstream,selection_path=path);self.assertIsNone(after.selected);self.assertFalse(after.status()['configured']);after.close()
 def test_upstream_bound_call_avoids_nested_paid_submission(self):
  c=BrokerClient(TOKEN,FEDERATED_BROKER_URL)
  with patch.object(c,'profiles_info',return_value={'profiles':Upstream().profiles_info()['profiles'][1:][0:1]}) as info:
   row=info.return_value['profiles'][0];row['upstream']=True
   bound=c.for_profile('old');self.assertEqual(bound.url,MULTI_BROKER_URL);self.assertEqual(bound.profile_revision,1)
if __name__=='__main__':unittest.main()
