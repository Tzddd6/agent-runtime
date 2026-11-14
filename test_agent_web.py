import json,tempfile,threading,time,unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch
from agent_runtime.web import Controller,make_server


class AgentWebTests(unittest.TestCase):
    def test_authenticated_agent_pause_resume_terminate_and_historical_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            controller=Controller(directory);server=make_server(controller,port=0,proxy_token='offline-control-token')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
            base='http://127.0.0.1:'+str(server.server_port)
            def request(path,value=None,csrf=True):
                headers={'X-AgentRT-Proxy-Token':'offline-control-token'}
                if csrf:headers['X-AgentRT-CSRF']=controller.csrf
                raw=None if value is None else json.dumps(value).encode()
                with urlopen(Request(base+path,data=raw,headers=headers),timeout=5) as response:return json.load(response)
            def wait(check):
                end=time.monotonic()+6
                while time.monotonic()<end:
                    value=request('/api/jobs/'+key)
                    if check(value):return value
                    time.sleep(.02)
                self.fail('Agent API state did not settle')
            tasks=[{'id':'a1','agent':'alpha','action':'sleep','seconds':.2},
                   {'id':'a2','agent':'alpha','depends_on':['a1']},
                   {'id':'b','agent':'beta','action':'sleep','seconds':3}]
            with patch('agent_runtime.scenarios.agent_tasks',return_value=(tasks,[])):
                key=request('/api/jobs',{'preset':'agents'})['id']
            route='/api/jobs/'+key+'/agents/'
            try:
                wait(lambda d:len(d['agents'])==2)
                with self.assertRaises(HTTPError) as denied:request(route+'alpha/pause',{},csrf=False)
                self.assertEqual(denied.exception.code,403)
                command=request(route+'alpha/pause',{});self.assertEqual(command['status'],'queued')
                wait(lambda d:any(a['id']=='alpha' and a['state']=='paused' for a in d['agents']))
                request(route+'alpha/resume',{});request(route+'beta/terminate',{})
                result=wait(lambda d:d['status'] not in ('running','cancelling'))
                self.assertEqual(result['states']['a2'],'succeeded');self.assertEqual(result['states']['b'],'cancelled')
                self.assertNotIn('_control',result)
                self.assertTrue(all(c['status']=='applied' for c in result['agent_controls']))
                with self.assertRaises(HTTPError):request(route+'alpha/resume',{})
                restored=Controller(directory).detail(key)
                self.assertTrue(restored['agents']);self.assertEqual(restored['agents'],result['agents'])
            finally:
                controller.jobs[key]['cancel'].set()
