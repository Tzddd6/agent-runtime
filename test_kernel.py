import json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from agent_runtime.runtime import run,DEFAULTS
from agent_runtime.scheduling import AdmissionScheduler
from agent_runtime.kernel import KernelCoordinator,service_for
from agent_runtime.kernel.managers import execute as execute_managed
from agent_runtime.memory_repository import MemoryRepository
from agent_runtime.sdk import KernelClient,Request,LLMQuery,ToolQuery,MemoryQuery,StorageQuery


GROUP=os.environ.get('AGENTRT_TEST_CGROUP')


class FixedClient:
    model='fixed-kernel-fixture';timeout=5
    def __init__(self):self.messages=[]
    def complete(self,messages,max_tokens):
        self.messages.append(messages)
        return {'content':'unchanged final answer','finish_reason':'stop','usage':{'prompt_tokens':7,'completion_tokens':3}}


class KernelTests(unittest.TestCase):
    def test_classification_is_derived_not_caller_selected(self):
        self.assertEqual(service_for({'action':'plugin','plugin':'math.calculate','service':'storage'}),'tool')
        self.assertEqual(service_for({'action':'plugin','plugin':'file.write'}),'storage')
        self.assertEqual(service_for({'action':'plugin','plugin':'memory.recall'}),'memory')
        self.assertEqual(service_for({'action':'plugin','plugin':'context.search'}),'context')
        self.assertEqual(service_for({'action':'plugin','plugin':None}),'tool')

    def test_round_robin_interleaves_ready_services_without_resource_bypass(self):
        budget=dict(DEFAULTS);scheduler=AdmissionScheduler('resource_fit',budget,DEFAULTS)
        kernel=KernelCoordinator('fixture',queue_policy='round_robin')
        ready=[{'id':'m1','action':'llm'},{'id':'m2','action':'llm'},
               {'id':'s','action':'plugin','plugin':'file.list'},{'id':'t','action':'plugin','plugin':'math.calculate'}]
        order=[]
        while ready:
            choice=kernel.choose(scheduler,ready,{k:0 for k in budget})
            task=next(t for t in ready if t['id']==choice.task_id)
            order.append(task['id']);kernel.dispatch(task,1);scheduler.started(task['id']);ready.remove(task)
        self.assertEqual(order,['m1','t','s','m2'])
        blocked=kernel.choose(scheduler,[{'id':'large','resources':{'memory_bytes':2*budget['memory_bytes']}}],{k:0 for k in budget})
        self.assertIsNone(blocked.task_id)

    def test_global_fifo_and_busy_agent_semantics_are_preserved(self):
        kernel=KernelCoordinator('fixture',queue_policy='round_robin')
        scheduler=AdmissionScheduler('fifo',dict(DEFAULTS),DEFAULTS)
        ready=[{'id':'storage','action':'plugin','plugin':'file.list'},{'id':'llm','action':'llm'}]
        decision=kernel.choose(scheduler,ready,{k:0 for k in DEFAULTS},busy_agents={'storage'})
        self.assertIsNone(decision.task_id)
        self.assertEqual(kernel.choose(scheduler,ready,{k:0 for k in DEFAULTS}).task_id,'storage')

    def test_duplicate_dispatch_is_refused_and_unknown_plugin_is_not_logged(self):
        kernel=KernelCoordinator('fixture');task={'id':'x','action':'plugin','plugin':'private-secret-like-name'}
        value=kernel.dispatch(task,1);self.assertEqual(value['operation'],'invalid_plugin')
        with self.assertRaises(RuntimeError):kernel.dispatch(task,1)
        self.assertNotIn('private-secret',json.dumps(kernel.snapshot([task],{'x':'waiting'})))

    def test_cancelled_and_retried_attempts_keep_distinct_call_identities(self):
        kernel=KernelCoordinator('fixture');task={'id':'x'}
        first=kernel.dispatch(task,1);kernel.event(task,'running',1,pid=100)
        kernel.event(task,'waiting',1);second=kernel.dispatch(task,2);kernel.event(task,'cancelled',2)
        self.assertNotEqual(first['id'],second['id'])
        calls=kernel.snapshot([task],{'x':'cancelled'})['calls']
        self.assertEqual([c['state'] for c in calls],['retrying','cancelled'])

    def test_manager_preserves_model_context_and_does_not_replay_errors(self):
        client=FixedClient();task={'action':'llm','messages':[{'role':'user','content':'same prompt'}],
              '_contexts':[{'text':'authorized'}],'_inputs':{'upstream':{'ok':True}}}
        with patch('agent_runtime.model_exchange.complete',side_effect=lambda task,msg:client.complete(msg,12)):
            result=execute_managed(task)
        self.assertEqual(result.model_call['content'],'unchanged final answer')
        body=json.loads(client.messages[0][-1]['content'])
        self.assertEqual(body['upstream_results'],task['_inputs'])

    def test_real_run_uses_managers_and_metadata_contains_no_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            client=FixedClient()
            states,events=run([{'id':'llm','action':'llm','messages':[{'role':'user','content':'private prompt fixture'}]},
                {'id':'calc','action':'plugin','plugin':'math.calculate','arguments':{'expression':'(12+3)*2'}}],
                output_dir=folder,quiet=True,model_client=client,cgroup_root=GROUP,kernel_queue_policy='round_robin')
            self.assertTrue(all(v=='succeeded' for v in states.values()))
            summary=json.loads((Path(folder)/events[-1]['workflow_id']/'summary.json').read_text())
            kernel=summary['kernel'];self.assertEqual(kernel['total_calls'],2)
            self.assertEqual({c['service'] for c in kernel['calls']},{'llm','tool'})
            self.assertTrue(all(c['pid'] and c['finished'] and c['state']=='succeeded' for c in kernel['calls']))
            self.assertNotIn('private prompt fixture',json.dumps(kernel));self.assertEqual(len(client.messages),1)
            self.assertTrue(all('syscall' in e for e in events if e['state']=='running'))

    def test_actual_memory_storage_and_context_managers(self):
        with tempfile.TemporaryDirectory() as folder:
            memory=MemoryRepository(Path(folder)/'memory','alice')
            client=KernelClient(Path(folder)/'client',user_identity='alice',memory_repository=memory,cgroup_root=GROUP)
            result=client.batch([
                Request('write','writer',StorageQuery('write',{'path':'note.txt','content':'verified content'})),
                Request('read','reader',StorageQuery('read',{'path':'note.txt'}),depends_on=('write',)),
                Request('remember','owner',MemoryQuery('write',{'content':'owned private note'})),
                Request('recall','owner',MemoryQuery('read'),depends_on=('remember',)),
                Request('other','other-agent',MemoryQuery('read'),depends_on=('remember',)),
                Request('context','reader',ToolQuery('context.search',{'query':'authorized','limit':3}),
                        contexts=({'text':'explicitly authorized material'},))])
            self.assertTrue(all(v=='succeeded' for v in result['states'].values()))
            self.assertIn('verified content',json.dumps(result['responses']['read']))
            self.assertIn('owned private note',json.dumps(result['responses']['recall']))
            self.assertNotIn('owned private note',json.dumps(result['responses']['other']))
            self.assertEqual({r['service'] for r in result['syscalls']},{'memory','storage','context'})

    def test_sdk_keeps_user_workspaces_separate_and_rejects_wrong_memory_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            alice=KernelClient(folder,user_identity='alice',cgroup_root=GROUP)
            bob=KernelClient(folder,user_identity='bob',cgroup_root=GROUP)
            self.assertNotEqual(alice.root,bob.root)
            alice.execute_request('writer',StorageQuery('write',{'path':'private.txt','content':'alice-only'}))
            result=bob.execute_request('reader',StorageQuery('read',{'path':'private.txt'}))
            self.assertNotIn('alice-only',json.dumps(result['responses']))
            memory=MemoryRepository(Path(folder)/'memory','alice')
            with self.assertRaises(ValueError):KernelClient(folder,user_identity='bob',memory_repository=memory)

    def test_sdk_cannot_override_identity_or_inject_transport_capabilities(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):KernelClient(folder,recovery_owner='somebody')
        with self.assertRaises(ValueError):MemoryQuery('read',{'user_id':'somebody'}).task()
        with self.assertRaises(ValueError):ToolQuery('memory.recall',{'agent_id':'somebody'}).task()
        with self.assertRaises(ValueError):ToolQuery('not.registered').task()

    def test_sdk_validates_model_and_dependencies_before_call(self):
        with self.assertRaises(ValueError):LLMQuery([{'role':'user','content':'x'}],max_tokens=0).task()
        with self.assertRaises(ValueError):LLMQuery([{'role':'user','content':{}}]).task()
        with tempfile.TemporaryDirectory() as folder:
            model=FixedClient();client=KernelClient(folder,model_client=model,cgroup_root=GROUP)
            with self.assertRaises(ValueError):client.batch([Request('x','a',LLMQuery([{'role':'user','content':'x'}]),depends_on=('missing',))])
            self.assertEqual(model.messages,[])

    def test_model_budget_still_prevents_supplier_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            model=FixedClient();client=KernelClient(folder,model_client=model,cgroup_root=GROUP,model_budget={'calls':0,'output_tokens':0})
            result=client.execute_request('a',LLMQuery([{'role':'user','content':'fixture'}]))
            self.assertEqual(result['states']['request'],'failed');self.assertEqual(model.messages,[])

    def test_uncertain_model_transport_is_not_replayed_by_sdk(self):
        from agent_runtime.model_gateway import ModelError
        class Failing(FixedClient):
            def complete(self,messages,max_tokens):
                self.messages.append(messages)
                raise ModelError('fixed timeout fixture',code='MODEL_TIMEOUT')
        with tempfile.TemporaryDirectory() as folder:
            model=Failing();client=KernelClient(folder,model_client=model,cgroup_root=GROUP)
            result=client.execute_request('a',LLMQuery([{'role':'user','content':'fixture'}]))
            self.assertEqual(result['states']['request'],'failed');self.assertEqual(len(model.messages),1)
            self.assertEqual(result['syscalls'][-1]['service'],'llm')

    def test_sdk_memory_acceptance_barrier_reaches_real_model_injection(self):
        with tempfile.TemporaryDirectory() as folder:
            model=FixedClient();memory=MemoryRepository(Path(folder)/'memory','alice')
            client=KernelClient(Path(folder)/'sdk',user_identity='alice',memory_repository=memory,model_client=model,cgroup_root=GROUP)
            result=client.batch([Request('write','a',MemoryQuery('write',{'content':'BARRIER_FIXTURE_NOTE'})),
                                 Request('answer','a',LLMQuery([{'role':'user','content':'use authorized memory'}]))])
            self.assertEqual(result['states'],{'write':'succeeded','answer':'succeeded'})
            self.assertEqual(len(model.messages),1);self.assertIn('BARRIER_FIXTURE_NOTE',json.dumps(model.messages))
            finished=next(e['time'] for e in result['events'] if e['task_id']=='write' and e['state']=='succeeded')
            started=next(e['time'] for e in result['events'] if e['task_id']=='answer' and e['state']=='running')
            self.assertGreaterEqual(started,finished)

    def test_failed_memory_barrier_blocks_model_and_does_not_charge(self):
        with tempfile.TemporaryDirectory() as folder:
            model=FixedClient();memory=MemoryRepository(Path(folder)/'memory','alice')
            client=KernelClient(Path(folder)/'sdk',user_identity='alice',memory_repository=memory,model_client=model,cgroup_root=GROUP)
            result=client.batch([Request('write','a',MemoryQuery('write',{'content':''})),
                                 Request('answer','b',LLMQuery([{'role':'user','content':'fixture'}]))])
            self.assertEqual(result['states']['write'],'failed');self.assertEqual(result['states']['answer'],'skipped')
            self.assertEqual(model.messages,[])

    def test_checkpoint_restores_call_history_and_confirmed_write_without_duplicate(self):
        if os.name!='posix':self.skipTest('POSIX checkpoint locks')
        with tempfile.TemporaryDirectory() as folder:
            checkpoint=Path(folder)/'checkpoint';tasks=[{'id':'done','message':'keep'}]
            states,events=run(tasks,quiet=True,output_dir=folder,cgroup_root=GROUP,checkpoint_dir=checkpoint,kernel_queue_policy='round_robin')
            again,resumed=run(tasks,quiet=True,output_dir=folder,cgroup_root=GROUP,checkpoint_dir=checkpoint,resume=True,kernel_queue_policy='round_robin')
            self.assertEqual(states,again);self.assertEqual(events,resumed)
            summary=json.loads((Path(folder)/events[-1]['workflow_id']/'summary.json').read_text())
            self.assertEqual(summary['kernel']['total_calls'],1)


if __name__=='__main__':unittest.main()
