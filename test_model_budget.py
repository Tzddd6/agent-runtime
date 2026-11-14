"""No supplier keys: admission races, failed calls, revocation and real Workers."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch
from agent_runtime.model_budget import ModelBudgetLedger,ModelBudgetExceeded
from agent_runtime.model_gateway import ModelGateway,ModelError
from agent_runtime.runtime import run


class ModelBudgetTests(unittest.TestCase):
    def test_concurrent_admission_reconciles_global_and_agent_limits(self):
        ledger=ModelBudgetLedger({'calls':3,'output_tokens':30})
        for name in ('a','b'):ledger.register(name,{'calls':2,'output_tokens':20})
        def admit(name):
            try:ledger.admit(name,10);return True
            except ModelBudgetExceeded:return False
        with ThreadPoolExecutor(8) as pool:results=list(pool.map(admit,['a','b']*20))
        self.assertEqual(sum(results),3)
        self.assertEqual(ledger.snapshot()['used'],{'calls':3,'output_tokens':30})
        self.assertEqual(sum(ledger.snapshot(a)['used']['calls'] for a in ('a','b')),3)
        self.assertEqual(ledger.snapshot()['rejected'],37)

    def test_bad_limits_and_registration_reset_are_rejected(self):
        for value in ([],False,{'calls':True},{'calls':-1},{'tokens':1},{'calls':101}):
            with self.assertRaises(ValueError):ModelBudgetLedger(value)
        ledger=ModelBudgetLedger({'calls':0});ledger.register('a')
        with self.assertRaises(ModelBudgetExceeded):ledger.admit('a',1)
        with self.assertRaises(ValueError):ledger.register('a',{'calls':1})
        with self.assertRaises(ValueError):ledger.admit('a',True)

    def test_gateway_rejects_without_provider_call_and_failed_call_is_not_refunded(self):
        class Client:
            timeout=1;model='offline-budget';calls=0
            def complete(self,*args):self.calls+=1;raise ModelError('offline uncertain failure')
        with tempfile.TemporaryDirectory() as tmp:
            ledger=ModelBudgetLedger();ledger.register('a',{'calls':1,'output_tokens':20});ledger.register('b')
            client=Client();gateway=ModelGateway(client,Path(tmp)/'calls.jsonl');gateway.budgets=ledger
            def request(token,messages=None):
                raw={'messages':messages or [{'role':'user','content':'private fixture'}],'max_tokens':10}
                try:
                    with urlopen(Request(gateway.url,data=json.dumps(raw).encode(),headers={'Authorization':'Bearer '+token}),timeout=3) as r:return r.status
                except HTTPError as e:return e.code
            try:
                a=gateway.issue('a');b=gateway.issue('b')
                self.assertEqual(request(gateway.token),403)
                self.assertEqual(request(a,[{'role':'invalid','content':'x'}]),400)
                self.assertEqual(ledger.snapshot()['used']['calls'],0)
                self.assertEqual(request(a),502)
                self.assertEqual(request(gateway.issue('a')),429) # New Attempt cannot reset ledger.
                self.assertEqual(client.calls,1)
                self.assertEqual(request(b),502)
                self.assertEqual(client.calls,2)
                text=(Path(tmp)/'calls.jsonl').read_text()
                self.assertNotIn('private fixture',text);self.assertNotIn(a,text)
            finally:gateway.close()

    def test_queued_revoked_attempt_does_not_spend(self):
        started=threading.Event();release=threading.Event()
        class Client:
            timeout=2;model='offline-budget'
            def complete(self,*args):started.set();release.wait(2);return {'content':'ok','usage':{}}
        with tempfile.TemporaryDirectory() as tmp:
            ledger=ModelBudgetLedger();ledger.register('a');ledger.register('b')
            gateway=ModelGateway(Client(),Path(tmp)/'calls.jsonl',concurrency=1);gateway.budgets=ledger
            a,b=gateway.issue('a'),gateway.issue('b')
            def request(token):
                try:
                    with urlopen(Request(gateway.url,data=json.dumps({'messages':[{'role':'user','content':'x'}]}).encode(),
                                          headers={'Authorization':'Bearer '+token}),timeout=4) as r:return r.status
                except HTTPError as e:return e.code
            try:
                with ThreadPoolExecutor(2) as pool:
                    first=pool.submit(request,a);self.assertTrue(started.wait(1))
                    second=pool.submit(request,b);gateway.revoke(b)
                    # Revocation cannot wait for the unrelated active request.
                    self.assertIn(second.result(1),(403,502));release.set()
                    self.assertEqual(first.result(3),200)
                self.assertEqual(ledger.snapshot('b')['used']['calls'],0)
            finally:release.set();gateway.close()

    def test_runtime_cross_task_agent_budget_does_not_block_independent_agent(self):
        class Client:
            timeout=2;model='offline-budget';calls=0
            def complete(self,*args):self.calls+=1;return {'content':'OK','usage':{'completion_tokens':1}}
        tasks=[{'id':key,'agent':owner,'action':'llm','max_tokens':16,
                'messages':[{'role':'user','content':'offline'}],**extra}
               for key,owner,extra in [('a1','a',{}),('a2','a',{'depends_on':['a1']}),('b','b',{})]]
        with tempfile.TemporaryDirectory() as tmp:
            client=Client();gateway=ModelGateway(client,Path(tmp)/'calls.jsonl')
            with patch('agent_runtime.runtime.ModelGateway.from_env',return_value=gateway):
                states,_=run(tasks,output_dir=tmp,quiet=True,agents=[{'id':'a','model_budget':{'calls':1}}],
                             cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertEqual(states,{'a1':'succeeded','a2':'failed','b':'succeeded'})
            self.assertEqual(client.calls,2)
            summary=json.loads(next(Path(tmp).glob('*/summary.json')).read_text())
            a=next(a for a in summary['agents'] if a['id']=='a')
            self.assertEqual(a['model_resources']['used'],{'calls':1,'output_tokens':16})
            self.assertEqual(a['model_resources']['rejected'],1)
            self.assertEqual(summary['model_resources']['used']['calls'],2)

    def test_web_budget_cannot_expand_service_cap_and_is_persisted(self):
        from agent_runtime.web import Controller
        import time
        with tempfile.TemporaryDirectory() as tmp:
            controller=Controller(tmp)
            with self.assertRaises(ValueError):controller.submit({'preset':'dynamic','model_budget':{'calls':33}})
            key=controller.submit({'preset':'dynamic','model_budget':{'calls':0,'output_tokens':0}})
            end=time.monotonic()+5
            while controller.detail(key)['status']=='running' and time.monotonic()<end:time.sleep(.02)
            result=Controller(tmp).detail(key)
            self.assertEqual(result['model_budget'],{'calls':0,'output_tokens':0})
            self.assertEqual(result['model_resources']['used']['calls'],0)

    def test_dynamic_agent_identity_cannot_bypass_workflow_budget(self):
        class Client:
            timeout=2;model='offline-budget';calls=0
            def complete(self,*args):self.calls+=1;return {'content':'OK','usage':{}}
        children=[{'id':'model'+str(i),'agent':'new_owner'+str(i),'action':'llm',
                   'messages':[{'role':'user','content':'offline'}]} for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            client=Client();gateway=ModelGateway(client,Path(tmp)/'calls.jsonl')
            with patch('agent_runtime.runtime.ModelGateway.from_env',return_value=gateway):
                states,_=run([{'id':'planner','spawn':children}],output_dir=tmp,quiet=True,
                             model_budget={'calls':1},cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertEqual(client.calls,1)
            self.assertEqual(sum(s=='failed' for s in states.values()),2)
            summary=json.loads(next(Path(tmp).glob('*/summary.json')).read_text())
            self.assertEqual(summary['model_resources']['used']['calls'],1)
            self.assertEqual(summary['model_resources']['rejected'],2)
