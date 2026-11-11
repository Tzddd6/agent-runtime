import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch
from agent_runtime.web import Controller, make_server


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controller=Controller(self.temp.name)
        self.server=make_server(self.controller,port=0,proxy_token='test-proxy-token')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def request(self,path,body=None,*,token='test-proxy-token',csrf=True,origin=None):
        headers={'X-AgentRT-Proxy-Token':token}
        if csrf:headers['X-AgentRT-CSRF']=self.controller.csrf
        if origin:headers['Origin']=origin
        data=None if body is None else json.dumps(body).encode()
        return urlopen(Request(self.base+path,data=data,headers=headers),timeout=5)

    def test_auth_and_csrf(self):
        with self.assertRaises(HTTPError) as e:self.request('/api/status',token='wrong')
        self.assertEqual(e.exception.code,403)
        with self.assertRaises(HTTPError) as e:self.request('/api/jobs',{'preset':'dynamic'},csrf=False)
        self.assertEqual(e.exception.code,403)
        with self.assertRaises(HTTPError):self.request('/api/jobs',{'preset':'dynamic'},origin='https://evil.example')
        status=json.load(self.request('/api/status'))
        self.assertNotIn('test-proxy-token',json.dumps(status))

    def test_real_workflow_and_results(self):
        index=self.request('/').read().decode()
        self.assertIn('运行控制台',index)
        key=json.load(self.request('/api/jobs',{'preset':'dynamic','workers':2,'policy':'resource_fit'}))['id']
        end=time.monotonic()+8
        while time.monotonic()<end:
            detail=json.load(self.request('/api/jobs/'+key))
            if detail['status'] not in ('running','cancelling'):break
            time.sleep(.05)
        self.assertEqual(detail['status'],'succeeded')
        self.assertEqual(detail['states']['review'],'succeeded')
        self.assertGreater(len(detail['events']),3)

    def test_cancel_and_single_flight(self):
        demo=Path(self.temp.name)/'demo.json'
        demo.write_text(json.dumps([{'id':'slow','action':'sleep','seconds':3}]))
        with patch('agent_runtime.web.DEMO',demo):
            key=json.load(self.request('/api/jobs',{'preset':'dynamic'}))['id']
            with self.assertRaises(HTTPError):self.request('/api/jobs',{'preset':'dynamic'})
        self.request('/api/jobs/'+key+'/cancel',{})
        end=time.monotonic()+8
        while time.monotonic()<end:
            detail=json.load(self.request('/api/jobs/'+key))
            if detail['status'] not in ('running','cancelling'):break
            time.sleep(.05)
        self.assertEqual(detail['status'],'cancelled')

    def test_no_arbitrary_commands(self):
        with self.assertRaises(HTTPError):self.request('/api/jobs',{'preset':'command','argv':['anything']})

    def test_monitor_assets_and_live_reservation_history(self):
        self.assertIn('cpu',self.request('/static/console.js').read().decode().lower())
        self.assertIn('text/css',self.request('/static/console.css').headers['Content-Type'])
        fixture=[{'id':'slow','agent':'observer-demo','action':'sleep','seconds':.65},
                 {'id':'after','depends_on':['slow']}]
        with patch('agent_runtime.scenarios.resource_tasks',return_value=fixture):
            key=json.load(self.request('/api/jobs',{'preset':'resources','policy':'aging_fit'}))['id']
        deadline=time.monotonic()+8
        snapshots=[]
        while time.monotonic()<deadline:
            detail=json.load(self.request('/api/jobs/'+key))
            if detail.get('monitor'):snapshots.append(detail['monitor'])
            if detail['status'] not in ('running','cancelling'):break
            time.sleep(.05)
        self.assertEqual(detail['status'],'succeeded')
        self.assertTrue(any(s['slots_used']==1 for s in snapshots))
        self.assertEqual(detail['monitor']['reserved']['cpu_milli'],0)
        self.assertGreaterEqual(len(detail['history']),2)
        status=json.load(self.request('/api/status'))
        self.assertIn('host',status['machine'])
        self.assertNotIn('monitor',status['jobs'][0])

    def test_key_configuration_never_echoes_secret(self):
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':''}):
            secret='mock-test-key-only-not-live'
            result=json.load(self.request('/api/model-config',{'key':secret}))
            self.assertTrue(result['configured'])
            status=json.load(self.request('/api/status'))
            self.assertTrue(status['model_configured'])
            self.assertNotIn(secret,json.dumps(status))
            self.request('/api/model-config',{'key':''})
            self.assertFalse(json.load(self.request('/api/status'))['model_configured'])


if __name__=='__main__':unittest.main()
