import json,os,tempfile,threading,time,unittest
from unittest.mock import patch
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError
from agent_runtime.web import Controller,make_server
from agent_runtime.model_broker import BrokerClient,make_server as broker_server
from agent_runtime.model_profiles import MultiModelBroker
from agent_runtime.tenants import TenantRouter


class ProfileWebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.called=[]
        def factory(provider,key,model):
            self.called.append(provider)
            class Client:
                def complete(self,*_):return {'content':'fixed profile answer','model':model,'usage':{'prompt_tokens':1,'completion_tokens':1},'finish_reason':'stop'}
            return Client()
        self.broker=MultiModelBroker('fixture-profile-control-token',profile_factory=factory);self.addCleanup(self.broker.close)
        self.supplier=self.serve(broker_server(self.broker,0))
        self.controller=Controller(self.temp.name,os.environ.get('AGENTRT_TEST_CGROUP'))
        self.controller.model_broker=BrokerClient('fixture-profile-control-token',self.supplier,local_test=True)
        self.router=TenantRouter(self.controller,'remote')
        with patch('agent_runtime.tenants.TenantRouter',return_value=self.router):
            self.url=self.serve(make_server(self.controller,port=0,proxy_token='fixture-web-token',tenant_routing=True))

    def serve(self,server):
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:'+str(server.server_port)

    def request(self,path,value=None,*,user='remote',csrf=True):
        controller=self.router.resolve(user)
        headers={'X-AgentRT-Proxy-Token':'fixture-web-token','X-AgentRT-User':user}
        if csrf:headers['X-AgentRT-CSRF']=controller.csrf
        request=Request(self.url+path,data=json.dumps(value).encode() if value is not None else None,headers=headers)
        with build_opener(ProxyHandler({})).open(request,timeout=5) as response:return json.load(response)

    def test_csrf_other_users_and_busy_tenant_cannot_change_api_profiles(self):
        value={'id':'gpt','label':'Fixture GPT','provider':'openai','model':'gpt-4.1-mini','key':'fixture-not-a-real-key-value'}
        with self.assertRaises(HTTPError) as denied:self.request('/api/model-profiles/save',value,csrf=False)
        self.assertEqual(denied.exception.code,403)
        with self.assertRaises(HTTPError):self.request('/api/model-profiles',user='alice')
        with self.assertRaises(HTTPError):self.request('/api/model-profiles/save',value,user='alice')
        alice=self.router.resolve('alice');alice.jobs['fixture']={'status':'running'}
        try:
            with self.assertRaises(HTTPError):self.request('/api/model-profiles/save',value)
            with self.assertRaises(HTTPError):self.request('/api/model-config',{'key':'fixture-not-a-real-key-value'})
        finally:alice.jobs.clear()
        self.assertFalse(self.broker.profiles)

    def test_save_select_then_real_runtime_workflow_is_bound_without_secret_output(self):
        key='fixture-not-a-real-key-value'
        self.request('/api/model-profiles/save',{'id':'gpt','label':'Fixture GPT','provider':'openai','model':'gpt-4.1-mini','key':key})
        self.assertEqual(self.called,[])
        self.request('/api/model-profiles/select',{'id':'gpt'})
        value=self.request('/api/jobs',{'preset':'llm','prompt':'fixed','workers':1,'policy':'fifo'})
        end=time.monotonic()+8
        while time.monotonic()<end:
            job=self.request('/api/jobs/'+value['id'])
            if job['status'] not in ('running','cancelling'):break
            time.sleep(.02)
        self.assertEqual(job['status'],'succeeded',json.dumps(job.get('events',[])[-2:],ensure_ascii=False))
        self.assertEqual(job['model_profile']['profile_id'],'gpt')
        self.assertEqual(self.called,['openai'])
        self.assertNotIn(key,json.dumps(job))
        self.assertNotIn(key,json.dumps(self.request('/api/model-profiles')))
        self.assertNotIn(key,json.dumps(self.controller.database.status()))


if __name__=='__main__':unittest.main()
