import io,json,threading,time,unittest
from unittest.mock import patch
from agent_runtime.model_profiles import MultiModelBroker
from agent_runtime.model_broker import ModelBroker,BrokerClient,make_server
from agent_runtime.model_gateway import OpenAIClient,DeepSeekClient,ModelError,inference_options
from agent_runtime.benchmark_transport import encode,complete as native_complete

TOKEN='fixture-control-token-not-a-real-secret'
KEY_A='fixture-provider-key-A-not-real'
KEY_B='fixture-provider-key-B-not-real'


def wait(broker,identity):
    end=time.monotonic()+3
    while time.monotonic()<end:
        result=broker.detail(identity)
        if result['status'] not in ('queued','running'):return result
        time.sleep(.005)
    raise AssertionError('Fixture call did not finish')


def profile(identity='openai',provider='openai',key=KEY_A):
    return {'id':identity,'label':'Fixture '+identity,'provider':provider,'model':'gpt-4.1-mini' if provider=='openai' else 'deepseek-flash','key':key}


class ProfileTests(unittest.TestCase):
    def broker(self,**kwargs):
        def factory(provider,key,model):
            class Client:
                def complete(self,messages,max_tokens):return {'content':provider,'model':model,'usage':{'prompt_tokens':1,'completion_tokens':1}}
            return Client()
        broker=MultiModelBroker(TOKEN,profile_factory=factory,**kwargs);self.addCleanup(broker.close);return broker

    def test_save_does_not_select_or_call_and_keys_are_never_returned(self):
        broker=self.broker();broker.save_profile(profile());broker.save_profile(profile('second','deepseek',KEY_B))
        self.assertFalse(broker.status()['configured']);self.assertEqual(broker.calls,{})
        output=json.dumps([broker.status(),broker.profiles_info()])
        for key in (KEY_A,KEY_B,TOKEN):self.assertNotIn(key,output)
        broker.select_profile({'id':'openai'})
        call=broker.submit({'request_id':'first','messages':[{'role':'user','content':'fixed'}]})
        self.assertEqual(wait(broker,call['call_id'])['result']['content'],'openai')

    def test_bound_profile_and_receipt_cannot_silently_change_vendor(self):
        broker=self.broker();broker.save_profile(profile());broker.save_profile(profile('second','deepseek',KEY_B))
        broker.select_profile({'id':'openai'})
        value={'request_id':'one','messages':[{'role':'user','content':'fixed'}],'profile_id':'openai'}
        accepted=broker.submit(value);wait(broker,accepted['call_id']);broker.select_profile({'id':'second'})
        self.assertTrue(broker.submit(value)['reused'])
        with self.assertRaises(ValueError):broker.submit({**value,'profile_id':'second'})
        self.assertEqual(len(broker.receipts),1)

    def test_active_calls_block_changes_and_profile_provider_is_immutable(self):
        entered=threading.Event();release=threading.Event()
        def factory(provider,key,model):
            class Client:
                def complete(self,*args):entered.set();release.wait(2);return {'content':'fixed','model':model}
            return Client()
        broker=MultiModelBroker(TOKEN,profile_factory=factory);self.addCleanup(broker.close)
        broker.save_profile(profile());broker.select_profile({'id':'openai'})
        call=broker.submit({'request_id':'active','messages':[{'role':'user','content':'fixed'}]})
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ModelError):broker.remove_profile({'id':'openai'})
            with self.assertRaises(ModelError):broker.save_profile(profile())
        finally:release.set()
        wait(broker,call['call_id'])
        with self.assertRaises(ValueError):broker.save_profile(profile(provider='deepseek',key=KEY_B))
        broker.remove_profile({'id':'openai'});self.assertFalse(broker.status()['configured']);self.assertIsNone(broker.selected)

    def test_profile_bounds_unknown_endpoints_and_metadata_secrets_are_rejected(self):
        broker=self.broker()
        with self.assertRaises(ValueError):broker.save_profile({**profile(),'base':'http://169.254.169.254'})
        with self.assertRaises(ValueError):broker.save_profile({**profile(),'label':KEY_A})
        with self.assertRaises(ValueError):broker.save_profile({**profile(),'provider':'unknown'})
        for index in range(7):broker.save_profile(profile('profile'+str(index)))
        with self.assertRaises(ValueError):broker.save_profile(profile('ninth'))

    def test_legacy_key_owner_is_not_read_or_reconfigured_when_adding_openai(self):
        class Legacy:
            calls=0
            def status(self):return {'configured':True,'model':'deepseek-flash'}
            def configure(self,*_):raise AssertionError('Do not move or replace existing keys')
            def complete(self,*_):self.calls+=1;return {'content':'legacy','model':'deepseek-flash'}
        legacy=Legacy();broker=self.broker(legacy_client=legacy);broker.save_profile(profile())
        self.assertTrue(broker.status()['configured']);self.assertEqual(legacy.calls,0)
        call=broker.submit({'request_id':'legacy','messages':[{'role':'user','content':'fixed'}]})
        self.assertEqual(wait(broker,call['call_id'])['result']['content'],'legacy')

    def test_sidecar_close_keeps_independent_legacy_owner_and_credential_alive(self):
        class Fixed:
            def complete(self,*_):return {'content':'legacy HTTP fixture','model':'deepseek-flash'}
        legacy=ModelBroker(TOKEN,client_factory=lambda _:Fixed());legacy.configure(KEY_B);self.addCleanup(legacy.close)
        server=make_server(legacy,0);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        client=BrokerClient(TOKEN,'http://127.0.0.1:'+str(server.server_port),local_test=True)
        broker=self.broker(legacy_client=client);broker.save_profile(profile())
        call=broker.submit({'request_id':'sidecar','messages':[{'role':'user','content':'fixed'}]})
        self.assertEqual(wait(broker,call['call_id'])['result']['content'],'legacy HTTP fixture')
        broker.close();self.assertTrue(client.status()['configured'])
        self.assertNotIn(KEY_B,json.dumps(legacy.status()))

    def test_http_profile_api_and_selection_are_authenticated_and_bound(self):
        broker=self.broker();server=make_server(broker,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        client=BrokerClient(TOKEN,'http://127.0.0.1:'+str(server.server_port),local_test=True)
        client.save_profile(profile());client.select_profile({'id':'openai'})
        bound=client.for_profile('openai','gpt-4.1-mini')
        client.save_profile(profile('second','deepseek',KEY_B));client.select_profile({'id':'second'})
        result=bound.complete([{'role':'user','content':'fixed'}],64)
        self.assertEqual(result['content'],'openai');self.assertNotIn(KEY_A,json.dumps(client.profiles_info()))
        client.save_profile({**profile(),'model':'gpt-4.1'})
        with self.assertRaises(ModelError):bound.complete([{'role':'user','content':'fixed'}],64)
        self.assertEqual(len(broker.receipts),1)


class OpenAIProtocolTests(unittest.TestCase):
    def test_openai_child_never_hands_its_key_to_legacy_owner(self):
        from types import SimpleNamespace
        from agent_runtime import model_call
        class Client:
            def __init__(self,*_,**__):pass
            def _complete_direct(self,*_):return {'content':'fixed'}
        request={'provider':'openai','key':KEY_A,'model':'gpt-4.1-mini','timeout':2,'base':'https://api.openai.com/v1','messages':[{'role':'user','content':'fixed'}],'max_tokens':64}
        output=io.StringIO()
        with patch.object(model_call.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))),patch.object(model_call.sys,'stdout',output),patch.object(model_call,'OpenAIClient',Client),patch.object(model_call,'handoff_existing_key') as handoff:
            model_call.main()
        handoff.assert_not_called();self.assertNotIn(KEY_A,output.getvalue())

    def test_openai_stream_preserves_markdown_and_final_usage_without_extra_requests(self):
        from agent_runtime.provider_stream import complete
        text='**标题**\n\n|a|b|\n|-|-|\n|1|2|\n```python\nprint(1)\n```\n\\(x^2\\)'
        captured=[];updates=[]
        class Response(io.BytesIO):
            headers={'Content-Type':'text/event-stream'}
            def __enter__(self):return self
            def __exit__(self,*_):self.close()
        class Opener:
            def open(self,request,timeout):
                if request.full_url.startswith('http://127.0.0.1:'):
                    updates.append(json.loads(request.data)['delta']);return Response(b'{}')
                captured.append(json.loads(request.data))
                chunks=[{'choices':[{'delta':{'content':text[:13]},'finish_reason':None}]},
                        {'choices':[{'delta':{'content':text[13:]},'finish_reason':'stop'}]},
                        {'choices':[],'usage':{'prompt_tokens':5,'completion_tokens':10,'total_tokens':15}}]
                return Response(b''.join(b'data: '+json.dumps(c).encode()+b'\n\n' for c in chunks)+b'data: [DONE]\n\n')
        with patch('urllib.request.build_opener',return_value=Opener()):
            result=complete(OpenAIClient(KEY_A),[{'role':'user','content':'fixed'}],64,{'url':'http://127.0.0.1:12345/progress','token':'fixture-token'})
        self.assertEqual(result['content'],text);self.assertEqual(''.join(updates),text)
        self.assertEqual(result['usage']['total_tokens'],15);self.assertEqual(len(captured),1)
        self.assertEqual(captured[0]['max_completion_tokens'],64);self.assertNotIn('thinking',captured[0])

    def test_official_endpoint_and_provider_parameters(self):
        client=OpenAIClient(KEY_A)
        self.assertEqual(inference_options(client,64),{'max_completion_tokens':64,'store':False})
        with self.assertRaises(ModelError):OpenAIClient(KEY_A,base='https://attacker.example/v1')
        with self.assertRaises(ModelError):inference_options(client,64,True)
        self.assertIn('thinking',inference_options(DeepSeekClient(KEY_A),64))

    def test_text_and_native_tools_use_openai_fields_and_never_deepseek_thinking(self):
        captured=[]
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*_):self.close()
        class Opener:
            def open(self,request,timeout):
                captured.append((request.full_url,json.loads(request.data)))
                message={'role':'assistant','content':'fixture'}
                return Response(json.dumps({'model':'gpt-4.1-mini','choices':[{'message':message,'finish_reason':'stop'}],'usage':{}}).encode())
        with patch('urllib.request.build_opener',return_value=Opener()):
            client=OpenAIClient(KEY_A)
            self.assertEqual(client._complete_direct([{'role':'user','content':'fixed'}],64)['content'],'fixture')
            native_complete(client,encode([{'role':'user','content':'fixed'}]),64)
        for url,body in captured:
            self.assertEqual(url,'https://api.openai.com/v1/chat/completions')
            self.assertEqual(body['max_completion_tokens'],64)
            self.assertNotIn('thinking',body);self.assertNotIn('max_tokens',body);self.assertFalse(body['store'])


if __name__=='__main__':unittest.main()
