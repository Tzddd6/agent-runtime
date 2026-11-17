import io,json,unittest,urllib.error
from unittest.mock import patch
from agent_runtime.model_gateway import AizexClient,ModelError
from agent_runtime.provider_responses import encode,decode,complete,error_metadata
KEY='synthetic-responses-key-not-real'
def response(text='{"ok":true}'):
 return {'model':'gpt-6-luna','status':'completed','output':[{'type':'reasoning','summary':[{'text':'synthetic-private-reasoning-marker'}]},{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}],'usage':{'input_tokens':11,'output_tokens':20,'total_tokens':31,'output_tokens_details':{'reasoning_tokens':7}}}
class ResponseTests(unittest.TestCase):
 def test_reasoning_is_not_delivered_but_usage_preserved(self):
  r=decode(response(),'model');self.assertEqual(r['content'],'{"ok":true}');self.assertEqual(r['usage']['completion_tokens'],20);self.assertEqual(r['usage']['completion_tokens_details']['reasoning_tokens'],7)
  self.assertNotIn('synthetic-private-reasoning-marker',json.dumps(r))
  literal='<think>user-requested literal markup</think>'
  self.assertEqual(decode(response(literal),'model')['content'],literal)
 def test_no_final_message_is_not_success(self):
  v=response();v['output']=v['output'][:1]
  with self.assertRaises(ModelError):decode(v,'model')
  v=response();v['status']='failed'
  with self.assertRaises(ModelError):decode(v,'model')
 def test_native_tool_ids_and_images(self):
  rows=[{'role':'system','content':'contract'},{'role':'user','content':[{'type':'text','text':'input'},{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,fixture'}}]},{'role':'assistant','content':None,'tool_calls':[{'id':'call_17','function':{'name':'lookup','arguments':'{}'}}]},{'role':'tool','tool_call_id':'call_17','content':'{"stock":1}'}]
  v=encode(rows);self.assertEqual(v['instructions'],'contract');self.assertEqual(v['input'][0]['content'][1]['type'],'input_image');self.assertEqual(v['input'][-1]['call_id'],'call_17')
  r=response();r['output']=[{'type':'function_call','call_id':'call_17','name':'lookup','arguments':'{}'}]
  parsed=json.loads(decode(r,'model',native=True)['content']);self.assertEqual(parsed['message']['tool_calls'][0]['id'],'call_17')
 def stream(self,events):
  class Reply(io.BytesIO):
   headers={'Content-Type':'text/event-stream'}
   def __enter__(self):return self
   def __exit__(self,*args):self.close()
  class Opener:
   def open(self,*args,**kwargs):return Reply(b''.join(b'data: '+json.dumps(e).encode()+b'\n\n' for e in events))
  return Opener()
 def test_stream_delivers_only_typed_text_and_requires_final_receipt(self):
  events=[{'type':'response.reasoning_text.delta','delta':'synthetic-private-reasoning-marker'},{'type':'response.output_text.delta','delta':'{"ok":true}'},{'type':'response.completed','response':response()}];updates=[]
  class Ack(io.BytesIO):
   def __enter__(self):return self
   def __exit__(self,*args):self.close()
  class Callback:
   def open(self,q,**kwargs):updates.append(json.loads(q.data)['delta']);return Ack(b'{}')
  with patch('agent_runtime.model_network.provider_opener',return_value=self.stream(events)),patch('urllib.request.build_opener',return_value=Callback()):
   r=complete(AizexClient(KEY),[{'role':'user','content':'fixture'}],128,{'url':'http://127.0.0.1:1/progress','token':'synthetic-capability'})
  self.assertEqual(''.join(updates),'{"ok":true}');self.assertEqual(r['content'],'{"ok":true}')
  with patch('agent_runtime.model_network.provider_opener',return_value=self.stream(events[:-1])),patch('urllib.request.build_opener',return_value=Callback()):
   with self.assertRaises(ModelError):complete(AizexClient(KEY),[{'role':'user','content':'fixture'}],128,{'url':'http://127.0.0.1:1/progress','token':'synthetic-capability'})
 def test_runtime_streams_cumulative_markdown_without_reasoning(self):
  import os,tempfile,threading,time
  from pathlib import Path
  from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
  from agent_runtime.runtime import run
  first='## 标题\n';last='**粗体**\n\n```python\nprint(1)\n```\n\n|A|B|\n|---|---|\n|1|2|\n\n$$x^2$$';text=first+last
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_POST(self):
    request=json.loads(self.rfile.read(int(self.headers['Content-Length'])));assert request['stream'] is True
    self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
    def send(event):self.wfile.write(b'data: '+json.dumps(event).encode()+b'\n\n');self.wfile.flush()
    send({'type':'response.reasoning_text.delta','delta':'PRIVATE-REASONING-MARKER'})
    send({'type':'response.output_text.delta','delta':first});time.sleep(.2)
    send({'type':'response.output_text.delta','delta':last});send({'type':'response.completed','response':response(text)})
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   with tempfile.TemporaryDirectory() as tmp:
    events=[];client=AizexClient(KEY,base='http://127.0.0.1:'+str(server.server_port),local_test=True)
    states,records=run([{'id':'assistant','action':'llm','messages':[{'role':'user','content':'synthetic format fixture'}],'max_tokens':256,'timeout':10}],quiet=True,output_dir=Path(tmp)/'runs',model_client=client,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'),output_observer=events.append,model_budget={'calls':1,'output_tokens':256})
    self.assertEqual(states['assistant'],'succeeded');self.assertGreaterEqual(len(events),2);self.assertEqual(events[-1]['text'],text)
    self.assertTrue(all(e['source']=='provider_content_delta' and 'delta' not in e for e in events));self.assertNotIn('PRIVATE-REASONING-MARKER',json.dumps(events))
    self.assertEqual(next(e['result']['result'] for e in records if e['state']=='succeeded'),text)
  finally:server.shutdown();server.server_close();thread.join()
 def test_stream_error_is_classified_without_provider_error_text(self):
  events=[{'type':'error','error':{'code':'server_error','message':'private-provider-sentinel'}}];traces=[]
  class Trace:
   def emit(self,stage,**values):traces.append({'stage':stage,**values})
  with patch('agent_runtime.transport_trace.Trace',return_value=Trace()),patch('agent_runtime.model_network.provider_opener',return_value=self.stream(events)):
   with self.assertRaises(ModelError) as caught:complete(AizexClient(KEY),[{'role':'user','content':'fixture'}],128,{'url':'http://127.0.0.1:1/progress','token':'synthetic'})
  self.assertEqual(caught.exception.code,'MODEL_UPSTREAM_FAILED');self.assertTrue(any(t['stage']=='provider_error' and t['error_kind']==2 for t in traces));self.assertNotIn('private-provider-sentinel',json.dumps(traces))
 def test_incomplete_output_stays_incomplete(self):
  v=response();v['status']='incomplete';self.assertEqual(decode(v,'model')['finish_reason'],'length')
 def test_http_error_categories_never_include_body_or_parameter_text(self):
  traces=[]
  class Trace:
   def emit(self,stage,**values):traces.append({'stage':stage,**values})
  body={'error':{'code':'unsupported_parameter','param':'reasoning.effort','message':'SECRET-HTTP-SENTINEL'}}
  error=urllib.error.HTTPError('https://fixture.invalid',400,'bad request',{},io.BytesIO(json.dumps(body).encode()))
  class Opener:
   def open(self,*args,**kwargs):raise error
  with patch('agent_runtime.transport_trace.Trace',return_value=Trace()),patch('agent_runtime.model_network.provider_opener',return_value=Opener()):
   with self.assertRaises(ModelError) as caught:complete(AizexClient(KEY),[{'role':'user','content':'fixture'}],128)
  self.assertEqual(caught.exception.code,'MODEL_UPSTREAM_FAILED')
  self.assertIn({'stage':'http_error','status':400,'error_kind':8,'error_param':5},traces)
  self.assertNotIn('SECRET-HTTP-SENTINEL',str(caught.exception)+json.dumps(traces))
  self.assertEqual(error_metadata({'code':'SECRET-HTTP-SENTINEL','param':'SECRET-HTTP-SENTINEL'}),{'error_kind':0,'error_param':0})
 def test_top_level_error_event_is_classified(self):
  self.assertEqual(error_metadata({'type':'error','code':'rate_limit_exceeded','param':None}),{'error_kind':1,'error_param':0})
 def test_multiline_sse_preserves_complex_format(self):
  text='**标题**\n\n$$x^2$$\n\n```python\nprint(1)\n```\n\n|A|B|\n|---|---|\n|1|2|'
  raw='\ufeff: ping\r\n\r\n'
  for event in ({'type':'response.output_text.delta','delta':text},{'type':'response.completed','response':response(text)}):
   raw+='event: message\r\n'+''.join('data: '+line+'\r\n' for line in json.dumps(event,ensure_ascii=False,indent=2).splitlines())+'\r\n'
  class Reply(io.BytesIO):
   headers={'Content-Type':'text/event-stream'}
   def __enter__(self):return self
   def __exit__(self,*args):self.close()
  class Opener:
   def open(self,*args,**kwargs):return Reply(raw.encode())
  with patch('agent_runtime.model_network.provider_opener',return_value=Opener()),patch('urllib.request.build_opener',return_value=self.stream([])):
   result=complete(AizexClient(KEY),[{'role':'user','content':'fixture'}],256,{'url':'http://127.0.0.1:1/progress','token':'synthetic'})
  self.assertEqual(result['content'],text)
if __name__=='__main__':unittest.main()
