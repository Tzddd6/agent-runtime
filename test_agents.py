import json,os,statistics,tempfile,threading,time,unittest
from unittest.mock import patch
from pathlib import Path
from agent_runtime.agents import RuntimeControl,AgentRegistry
from agent_runtime.runtime import run
from agent_runtime.context import ContextStore
from agent_runtime.ipc import Broker
from agent_runtime.model_gateway import ModelGateway


class AgentTests(unittest.TestCase):
    def start(self,tasks,specs=None,workers=2,cgroup=None):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        control=RuntimeControl();cancel=threading.Event();samples=[];out={}
        def execute():
            try:out['result']=run(tasks,workers,output_dir=temporary.name,agents=specs,control=control,
                observer=samples.append,cgroup_root=cgroup,cancel_event=cancel,quiet=True)
            except BaseException as e:out['error']=e
        thread=threading.Thread(target=execute);thread.start()
        def cleanup():cancel.set();thread.join(6)
        self.addCleanup(cleanup)
        return control,samples,out,thread,Path(temporary.name)

    def wait(self,predicate,timeout=8):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            if predicate():return
            time.sleep(.01)
        self.fail('Timed out waiting for Agent lifecycle evidence')

    def test_pause_drains_then_resume_and_other_agent_keeps_running(self):
        tasks=[{'id':'a1','agent':'alpha','action':'sleep','seconds':.3},
               {'id':'a2','agent':'alpha','depends_on':['a1']},
               {'id':'b1','agent':'beta','action':'sleep','seconds':.65}]
        control,samples,out,thread,_=self.start(tasks)
        self.wait(lambda:any(a['state']=='running' and a['id']=='alpha' for s in samples for a in s['agents']))
        pause=control.request('alpha','pause')
        self.wait(lambda:samples and any(a['id']=='alpha' and a['state']=='paused' for a in samples[-1]['agents']))
        self.wait(lambda:samples[-1]['tasks'][2]['state']=='succeeded')
        self.assertEqual(samples[-1]['tasks'][1]['state'],'waiting')
        self.assertEqual(next(a for a in samples[-1]['agents'] if a['id']=='alpha')['reserved']['cpu_milli'],0)
        control.request('alpha','resume');thread.join(5)
        self.assertFalse(thread.is_alive());self.assertNotIn('error',out)
        self.assertTrue(all(s=='succeeded' for s in out['result'][0].values()))
        self.assertEqual(next(c for c in control.history() if c['id']==pause['id'])['status'],'applied')
        self.assertTrue(all(a['state']=='completed' for a in samples[-1]['agents']))
        with self.assertRaises(ValueError):control.request('alpha','resume')

    def test_terminate_kills_owned_attempts_and_future_tasks_without_harming_peer(self):
        tasks=[{'id':'a1','agent':'alpha','action':'sleep','seconds':4,'retries':3},
               {'id':'a2','agent':'alpha','depends_on':['a1']},
               {'id':'b','agent':'beta','action':'sleep','seconds':.45,
                'spawn':[{'id':'late','agent':'alpha','depends_on':['b']}]}]
        control,samples,out,thread,root=self.start(tasks)
        self.wait(lambda:any(a['id']=='alpha' and a['active_tasks'] for s in samples for a in s['agents']))
        control.request('alpha','terminate');thread.join(5)
        self.assertFalse(thread.is_alive());self.assertNotIn('error',out)
        states,events=out['result'];self.assertEqual(states,{'a1':'cancelled','a2':'cancelled','b':'succeeded','late':'cancelled'})
        self.assertEqual(sum(e['task_id']=='a1' and e['state']=='running' for e in events),1)
        summary=json.loads(next(root.glob('*/summary.json')).read_text())
        alpha=next(a for a in summary['agents'] if a['id']=='alpha')
        self.assertEqual(alpha['state'],'terminated');self.assertEqual(alpha['reserved']['cpu_milli'],0)
        self.assertFalse(alpha['active_tasks'])

    def test_agent_budget_and_concurrency_are_distinct_from_global_budget(self):
        resources={'cpu_milli':250,'memory_bytes':64*1024**2,'pids':8}
        tasks=[{'id':str(i),'agent':'alpha','action':'sleep','seconds':.12,'resources':resources} for i in range(3)]
        control,samples,out,thread,_=self.start(tasks,[{'id':'alpha','max_concurrency':2,
            'budget':{'cpu_milli':250,'memory_bytes':128*1024**2,'pids':16}}],workers=3)
        thread.join(6);self.assertNotIn('error',out)
        self.assertTrue(all(len(a['active_tasks'])<=1 and a['reserved']['cpu_milli']<=250 for s in samples for a in s['agents']))
        self.assertTrue(all(v=='succeeded' for v in out['result'][0].values()))
        with tempfile.TemporaryDirectory() as directory:
            states,_=run([{'id':'big','agent':'small'}],agents=[{'id':'small','budget':{'cpu_milli':100}}],output_dir=directory,quiet=True)
        self.assertEqual(states['big'],'unschedulable')

    def test_revoking_agent_removes_context_authority_and_mailbox_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ContextStore(Path(directory));broker=Broker(store,['alpha','beta'])
            try:
                a,b=broker.issue('alpha'),broker.issue('beta')
                receipt=broker.dispatch(a,{'op':'send','to':'beta','data':{'x':1},'request_id':'one'})
                broker.terminate_agent('alpha');store.revoke_agent('alpha')
                with self.assertRaises(PermissionError):broker.dispatch(a,{'op':'receive'})
                with self.assertRaises(PermissionError):store.get(receipt['ref'],'alpha')
                self.assertEqual(store.get(receipt['ref'],'beta'),{'x':1})
                with self.assertRaises(PermissionError):broker.dispatch(b,{'op':'send','to':'alpha','data':1,'request_id':'two'})
            finally:broker.close()

    def test_invalid_registration_is_rejected(self):
        budget={'cpu_milli':1000,'memory_bytes':128*1024**2,'pids':32}
        for specs in ([{'id':'../escape'}],[{'id':'a','budget':{'cpu_milli':2000}}],
                      [{'id':'a','max_concurrency':0}],[{'id':'a'},{'id':'a'}]):
            with self.assertRaises(ValueError):AgentRegistry(budget,specs)

    def test_agent_journal_failure_still_closes_model_gateway(self):
        class Client:
            timeout=2;model='offline'
            def complete(self,*args):return {'content':'OK','usage':{}}
        with tempfile.TemporaryDirectory() as directory:
            gateway=ModelGateway(Client(),Path(directory)/'model.jsonl')
            original=AgentRegistry.synchronize
            def fail(registry,*args,**kwargs):
                if kwargs.get('finished'):raise OSError('fixture full Agent journal')
                return original(registry,*args,**kwargs)
            with patch('agent_runtime.runtime.ModelGateway.from_env',return_value=gateway),\
                 patch.object(AgentRegistry,'synchronize',fail),\
                 patch.object(gateway,'close',wraps=gateway.close) as close:
                with self.assertRaisesRegex(RuntimeError,'TEARDOWN_FAILED'):
                    run([{'id':'m','action':'llm','messages':[{'role':'user','content':'offline'}]}],output_dir=directory,quiet=True)
                close.assert_called_once()

    @unittest.skipUnless(os.name=='posix' and os.environ.get('AGENTRT_TEST_CGROUP'),'requires delegated Linux cgroup')
    def test_linux_aggregate_agent_cgroup_and_cleanup(self):
        limits={'cpu_milli':200,'memory_bytes':96*1024**2,'pids':16}
        tasks=[{'id':str(i),'agent':'owner','action':'load','seconds':2,'memory_mib':16,'resources':limits} for i in range(2)]
        c,s,o,t,root=self.start(tasks,[{'id':'owner','max_concurrency':2,
            'budget':{'cpu_milli':400,'memory_bytes':256*1024**2,'pids':40}}],cgroup=os.environ['AGENTRT_TEST_CGROUP'])
        t.join(8);self.assertNotIn('error',o);self.assertFalse(t.is_alive())
        agents=[a for sample in s for a in sample['agents']]
        self.assertTrue(any(len(a['active_tasks'])==2 for a in agents))
        live=[a['metrics']['cpu_cores'] for a in agents if a['active_tasks'] and a['metrics'].get('cpu_cores') is not None]
        self.assertGreater(statistics.median(live),.15);self.assertLess(statistics.median(live),.8)
        final=agents[-1];self.assertGreater(final['metrics']['cpu_total_seconds'],.2)
        self.assertGreater(final['metrics']['memory_peak_bytes'],32*1024**2)
        self.assertEqual(final['reserved']['cpu_milli'],0)
        self.assertFalse(Path(final['cgroup_path']).exists())
