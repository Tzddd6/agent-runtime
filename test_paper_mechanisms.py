"""Paper-derived allocation and reference transport, including failure boundaries."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent_runtime.scheduling import AdmissionScheduler
from agent_runtime.context import ContextStore
from agent_runtime.ipc import Broker
from agent_runtime.runtime import run
from agent_runtime.executor import Executor


class PaperMechanismTests(unittest.TestCase):
    def test_drf_reproduces_paper_two_user_allocation(self):
        budget={'cpu':9,'memory':18,'pids':1000}
        scheduler=AdmissionScheduler('drf',budget,{'cpu':1,'memory':1,'pids':1})
        ready=[{'id':f'{owner}{i}','agent':owner,'resources':request}
               for i in range(8) for owner,request in
               [('B',{'cpu':3,'memory':1}),('A',{'cpu':1,'memory':4})]]
        allocations={a:{k:0 for k in budget} for a in ('A','B')}
        used={k:0 for k in budget};selected=[]
        while True:
            choice=scheduler.choose(ready,used,agent_allocations=allocations,now=0)
            if choice.task_id is None:break
            task=next(t for t in ready if t['id']==choice.task_id)
            selected.append(task['agent']);ready.remove(task);scheduler.started(task['id'])
            for k in budget:
                value=task['resources'].get(k,1)
                used[k]+=value;allocations[task['agent']][k]+=value
        self.assertEqual(selected,['B','A','A','B','A'])
        self.assertEqual(allocations['A']['memory']/18,2/3)
        self.assertEqual(allocations['B']['cpu']/9,2/3)

    def test_drf_drains_low_share_head_instead_of_backfilling(self):
        budget={'cpu':4,'memory':100}
        scheduler=AdmissionScheduler('drf',budget,{'cpu':1,'memory':10})
        ready=[{'id':'a','agent':'A'},
               {'id':'b','agent':'B','resources':{'cpu':2}}]
        allocated={'A':{'cpu':3,'memory':30}}
        self.assertEqual(scheduler.choose(ready,allocated['A'],agent_allocations=allocated).draining_task_id,'b')
        # Paused/budget-blocked Agents cannot reserve the whole queue.
        self.assertEqual(scheduler.choose(ready,allocated['A'],agent_allocations=allocated,
                                         busy_agents=['B']).task_id,'a')
        with self.assertRaises(ValueError):scheduler.choose(ready,allocated['A'])

    def test_reference_capability_isolation_and_revocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ContextStore(Path(tmp));ref=store.put({'secret':'private'},['owner'])
            broker=Broker(store,['owner','reader','outsider'])
            try:
                owner=broker.issue('owner');reader=broker.issue('reader');outsider=broker.issue('outsider')
                with self.assertRaises(PermissionError):broker.dispatch(reader,{'op':'get','ref':ref})
                with self.assertRaises(PermissionError):store.grant(ref,'outsider','reader')
                store.grant(ref,'owner','reader')
                self.assertEqual(broker.dispatch(reader,{'op':'get','ref':ref})['data'],{'secret':'private'})
                with self.assertRaises(PermissionError):broker.dispatch(outsider,{'op':'get','ref':ref})
                broker.revoke(reader)
                with self.assertRaises(PermissionError):broker.dispatch(reader,{'op':'get','ref':ref})
                self.assertEqual(broker.dispatch(owner,{'op':'get','ref':ref})['data']['secret'],'private')
            finally:broker.close()

    def test_real_workers_receive_refs_and_materialize_identical_inputs(self):
        large='上下文abc '*8000
        tasks=[{'id':'source','agent':'producer','message':{'text':large}},
               {'id':'review','agent':'consumer','action':'review_document',
                'depends_on':['source'],'inputs':['source'],'contexts':[{'text':large}]}]
        payloads={};launch=Executor.launch
        def capture(executor,payload,*args,**kwargs):
            payloads[payload['id']]=dict(payload)
            return launch(executor,payload,*args,**kwargs)
        with tempfile.TemporaryDirectory() as tmp,patch.object(Executor,'launch',capture):
            states,events=run(tasks,output_dir=tmp,quiet=True,policy='drf',
                              cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue(all(s=='succeeded' for s in states.values()),events)
            payload=payloads['review']
            for key in ('contexts','_contexts','_inputs'):self.assertNotIn(key,payload)
            self.assertLess(len(json.dumps(payload)),2048)
            result=next(e['result'] for e in events if e['task_id']=='review' and e['state']=='succeeded')
            self.assertEqual(result['result']['report'],{'source':{'text':large}})
            self.assertEqual(result['contexts'],[{'text':large}])
            summary=json.loads(next(Path(tmp).glob('*/summary.json')).read_text(encoding='utf-8'))
            self.assertEqual(summary['dataflow'],{'context_ref_deliveries':1,'input_ref_deliveries':1})

    def test_drf_web_entry_uses_same_runtime_policy(self):
        from agent_runtime.web import Controller
        with tempfile.TemporaryDirectory() as tmp:
            controller=Controller(tmp)
            with patch('agent_runtime.web.run',return_value=({'task':'succeeded'},[])) as runtime:
                key=controller.submit({'preset':'dynamic','policy':'drf','workers':2})
                import time
                end=time.monotonic()+5
                while controller.detail(key)['status']=='running' and time.monotonic()<end:time.sleep(.01)
                self.assertEqual(runtime.call_args.kwargs['policy'],'drf')
