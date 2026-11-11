import json
import io
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from unittest.mock import patch
from types import SimpleNamespace
from agent_runtime.model_broker import ModelBroker,BrokerClient,make_server
from agent_runtime.model_gateway import ModelError,ModelGateway
from agent_runtime.model_call import handoff_existing_key


TOKEN='fixture-control-token-not-live'
KEY='fixture-supplier-key-not-live'
MESSAGES=[{'role':'user','content':'private fixture prompt'}]


def wait(broker,identity):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        detail=broker.detail(identity)
        if detail['status'] not in ('queued','running'):return detail
        time.sleep(.01)
    raise AssertionError('fixture call did not finish')


class BrokerTests(unittest.TestCase):
    def test_supplier_handoff_is_disabled_for_mock_and_local_test_calls(self):
        from agent_runtime import model_call
        class Client:
            def __init__(self,*args,**kwargs):pass
            def _complete_direct(self,*args):return {'content':'fixture'}
        for base,local_test,expected in [('http://127.0.0.1:1',True,False),
                ('https://api.deepseek.com',True,False),('https://api.deepseek.com',False,True)]:
            payload={'key':KEY,'model':'fixture','timeout':1,'base':base,'local_test':local_test,
                     'messages':MESSAGES,'max_tokens':1}
            stdin=SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))
            with patch.object(model_call.sys,'stdin',stdin),patch.object(model_call.sys,'stdout',io.StringIO()), \
                    patch.object(model_call,'DeepSeekClient',Client),patch.object(model_call,'handoff_existing_key') as hook:
                model_call.main()
                self.assertEqual(hook.called,expected)

    def make_broker(self,**kwargs):
        class Client:
            def complete(self,messages,max_tokens):
                return {'content':'answer','usage':{'prompt_tokens':3,'completion_tokens':1},'model':'fixture',
                        'unexpected_key':KEY}
        broker=ModelBroker(TOKEN,client_factory=lambda key:Client(),**kwargs)
        self.addCleanup(broker.close)
        return broker

    def server(self,broker):
        server=make_server(broker,0)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}'

    def test_auth_and_configuration_never_return_supplier_key(self):
        broker=self.make_broker();url=self.server(broker)
        with self.assertRaises(HTTPError) as denied:urlopen(url+'/status')
        self.assertEqual(denied.exception.code,403)
        client=BrokerClient(TOKEN,url,local_test=True)
        self.assertEqual(client.configure(KEY),{'configured':True})
        self.assertNotIn(KEY,json.dumps(client.status()))
        result=client.complete(MESSAGES)
        self.assertEqual(result['content'],'answer')
        self.assertNotIn(KEY,json.dumps(result))
        self.assertNotIn('private fixture prompt',json.dumps(broker.receipts))

    def test_dedup_survives_response_eviction_and_capacity_is_bounded(self):
        broker=self.make_broker(history_limit=1,max_requests=2)
        broker.configure(KEY)
        request={'request_id':'one','messages':MESSAGES}
        first=broker.submit(request)['call_id'];wait(broker,first)
        second=broker.submit({**request,'request_id':'two'})['call_id'];wait(broker,second)
        self.assertEqual(len(broker.calls),1)
        self.assertEqual(broker.submit(request),{'call_id':first,'reused':True})
        with self.assertRaises(KeyError):broker.detail(first)
        with self.assertRaises(ValueError):broker.submit({**request,'messages':[{'role':'user','content':'changed'}]})
        with self.assertRaises(ModelError):broker.submit({**request,'request_id':'three'})

    def test_cancellation_stops_queued_call_without_provider_invocation(self):
        started=threading.Event();release=threading.Event();invocations=[]
        class Client:
            def complete(self,*args):
                invocations.append(1);started.set()
                while not release.is_set() and not self.cancel_event.is_set():time.sleep(.01)
                return {'content':'answer'}
        broker=ModelBroker(TOKEN,client_factory=lambda key:Client(),concurrency=1)
        self.addCleanup(broker.close);broker.configure(KEY)
        first=broker.submit({'request_id':'one','messages':MESSAGES})['call_id']
        self.assertTrue(started.wait(2))
        with self.assertRaises(ModelError):broker.configure('')
        second=broker.submit({'request_id':'two','messages':MESSAGES})['call_id']
        broker.cancel(second)
        self.assertEqual(wait(broker,second)['status'],'cancelled')
        self.assertEqual(len(invocations),1)
        release.set();wait(broker,first)

    def test_global_slots_queue_and_failure_redaction(self):
        lock=threading.Lock();active=[0];peak=[0]
        class Client:
            def complete(self,*args):
                with lock:active[0]+=1;peak[0]=max(peak[0],active[0])
                time.sleep(.08)
                with lock:active[0]-=1
                raise RuntimeError(KEY+' '+MESSAGES[0]['content'])
        broker=ModelBroker(TOKEN,client_factory=lambda key:Client(),concurrency=2,max_pending=4)
        self.addCleanup(broker.close);broker.configure(KEY)
        calls=[broker.submit({'request_id':str(i),'messages':MESSAGES})['call_id'] for i in range(4)]
        with self.assertRaises(ModelError):broker.submit({'request_id':'overflow','messages':MESSAGES})
        results=[wait(broker,i) for i in calls]
        self.assertEqual(peak[0],2)
        self.assertNotIn(KEY,json.dumps(results))
        self.assertNotIn('private fixture prompt',json.dumps(results))

    def test_client_cancel_propagates_and_no_paid_post_is_retried(self):
        entered=threading.Event()
        class Client:
            def complete(self,*args):
                entered.set()
                while not self.cancel_event.is_set():time.sleep(.01)
                raise RuntimeError('cancelled')
        broker=ModelBroker(TOKEN,client_factory=lambda key:Client())
        self.addCleanup(broker.close);broker.configure(KEY)
        client=BrokerClient(TOKEN,self.server(broker),local_test=True)
        client.cancel_event=threading.Event();errors=[]
        def invoke():
            try:client.complete(MESSAGES)
            except ModelError:errors.append(1)
        worker=threading.Thread(target=invoke);worker.start()
        self.assertTrue(entered.wait(2));start=time.monotonic();client.cancel_event.set();worker.join(2)
        self.assertFalse(worker.is_alive());self.assertTrue(errors)
        self.assertLess(time.monotonic()-start,2)
        self.assertEqual(len(broker.receipts),1)

    def test_model_gateway_selects_broker_without_supplier_environment_key(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ,{'AGENTRT_MODEL_BROKER_URL':'http://127.0.0.1:18886',
                                        'AGENTRT_PROXY_TOKEN':TOKEN,'DEEPSEEK_API_KEY':''}):
                gateway=ModelGateway.from_env(Path(directory)/'calls.jsonl')
                try:self.assertIsInstance(gateway.client,BrokerClient)
                finally:gateway.close()
        with self.assertRaises(ModelError):BrokerClient(TOKEN,'https://example.com')

    @unittest.skipUnless(os.name=='posix','private owner permissions use Linux')
    def test_handoff_requires_private_marker_and_never_writes_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);root.chmod(0o700)
            marker=root/'broker-handoff.json'
            self.assertFalse(handoff_existing_key(KEY,path=marker))
            marker.write_text(json.dumps({'url':'http://127.0.0.1:18886','nonce':'fixture-nonce-123456'}));marker.chmod(0o600)
            proxy=root/'console.env';proxy.write_text('AGENTRT_PROXY_TOKEN='+TOKEN+'\n');proxy.chmod(0o600)
            class Receiver:
                def configure(self,key):
                    self.received=key
                    return {'configured':True}
            receiver=Receiver()
            self.assertTrue(handoff_existing_key(KEY,path=marker,client_factory=lambda token:receiver))
            self.assertEqual(receiver.received,KEY)
            self.assertFalse(marker.exists())
            self.assertNotIn(KEY,proxy.read_text())
            marker.write_text(json.dumps({'url':'http://127.0.0.1:18886','nonce':'fixture-nonce-123456'}));marker.chmod(0o644)
            with self.assertRaises(ModelError):handoff_existing_key(KEY,path=marker,client_factory=lambda token:receiver)


if __name__=='__main__':unittest.main()
