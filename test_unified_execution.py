import json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agent_runtime.runtime import run
from agent_runtime.model_gateway import ModelGateway


class UnifiedExecutionTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform=='linux','real Linux process execution')
    def test_one_agent_owns_model_tool_process_and_context_with_one_resource_ledger(self):
        class Client:
            model='offline';timeout=2
            def complete(self,messages,max_tokens):return {'content':'model result','finish_reason':'stop','usage':{'prompt_tokens':5,'completion_tokens':3}}
        limits={'cpu_milli':500,'memory_bytes':96*1024**2,'pids':16}
        def task(key,action,dependency=None,**kwargs):
            return {'id':key,'agent':'operator','action':action,'resources':limits,'depends_on':[dependency] if dependency else [],'inputs':[dependency] if dependency else [],**kwargs}
        tasks=[task('context','echo',message='original',contexts=[{'text':'original facts'}]),
               task('model','llm','context',max_tokens=128,messages=[{'role':'user','content':'use upstream'}]),
               task('tool','plugin','model',plugin='math.calculate',arguments={'expression':'(2+3)*4'}),
               task('process','command','tool',argv=[sys.executable,'-c','print("process checked")'])]
        with tempfile.TemporaryDirectory() as folder,patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda path:ModelGateway(Client(),path)):
            states,events=run(tasks,quiet=True,output_dir=folder,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue(all(value=='succeeded' for value in states.values()))
            started=[e for e in events if e['state']=='running']
            self.assertEqual([e['kind'] for e in started],['runtime','model','tool','process'])
            self.assertEqual({e['agent'] for e in started},{'operator'})
            self.assertEqual(len({e['pid'] for e in started}),4)
            if os.environ.get('AGENTRT_TEST_CGROUP'):self.assertTrue(all(e['cgroup_path'] for e in started))
            summary=json.loads(next(Path(folder).glob('*/summary.json')).read_text())
            owner=summary['agents'][0];self.assertEqual(owner['model_calls'],1)
            self.assertEqual(owner['reserved'],{'cpu_milli':0,'memory_bytes':0,'pids':0})
            self.assertTrue(owner['context_refs']);self.assertGreater(summary['ipc_stats']['requests'],0)
            self.assertEqual(summary['model_resources']['used']['calls'],1)

    def test_conservative_repair_default_keeps_full_source_contract(self):
        from agent_runtime.repair_loop import tasks
        self.assertEqual(tasks('修复pricing','pricing')[0]['repair_protocol'],'full')
        self.assertEqual(tasks('修复pricing','pricing',protocol='compact')[0]['repair_protocol'],'compact')


if __name__=='__main__':unittest.main()
