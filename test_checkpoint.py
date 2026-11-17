import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent_runtime.runtime import run
from agent_runtime.executor import Executor
from agent_runtime.checkpoint import WorkflowCheckpoint,RecoveryBlocked,read_checkpoint,recovery_view


@unittest.skipUnless(os.name=='posix','durable workflow needs POSIX file locks')
class CheckpointTests(unittest.TestCase):
    def options(self,tmp,**extra):return dict(output_dir=Path(tmp)/'runs',checkpoint_dir=Path(tmp)/'checkpoint',recovery_owner='alice',quiet=True,**extra)

    def interrupt(self,tasks,options,at):
        original=Executor.launch
        def launch(executor,payload,*args):
            if payload['id']==at:raise KeyboardInterrupt('injected coordinator interruption')
            return original(executor,payload,*args)
        with patch.object(Executor,'launch',launch),self.assertRaises(KeyboardInterrupt):run(tasks,max_workers=1,**options)

    def test_completed_write_is_not_repeated_and_dependency_output_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Path(tmp)/'workspace';workspace.mkdir()
            tasks=[{'id':'write','action':'command','argv':['/bin/sh','-c',f'echo line >> {workspace}/count']},
                   {'id':'pause','action':'sleep','seconds':.01,'depends_on':['write']},
                   {'id':'answer','message':'done','depends_on':['pause'],'inputs':['pause']}]
            options=self.options(tmp,workspace=workspace)
            self.interrupt(tasks,options,'pause')
            self.assertEqual((workspace/'count').read_text(),'line\n')
            states,events=run(tasks,max_workers=1,resume=True,**options)
            self.assertTrue(all(v=='succeeded' for v in states.values()))
            self.assertEqual((workspace/'count').read_text(),'line\n')
            self.assertEqual(sum(e['task_id']=='write' and e['state']=='running' for e in events),1)
            self.assertEqual(read_checkpoint(options['checkpoint_dir'],'alice')['attempts']['pause'],2)

    def test_uncertain_command_or_model_is_never_automatically_replayed(self):
        for task in [{'id':'unsafe','action':'command','argv':['/bin/true']},
                     {'id':'unsafe','action':'llm','messages':[{'role':'user','content':'private synthetic prompt'}]}]:
            with self.subTest(task=task['action']),tempfile.TemporaryDirectory() as tmp:
                options=self.options(tmp)
                # Model client setup is explicit and never invokes a provider.
                class Client:
                    model='fixed';timeout=3
                    def complete(self,*args):raise AssertionError('No provider request allowed')
                if task['action']=='llm':options['model_client']=Client()
                self.interrupt([task],options,'unsafe')
                with patch.object(Executor,'launch',side_effect=AssertionError('Unsafe replay')):
                    with self.assertRaises(RecoveryBlocked):run([task],max_workers=1,resume=True,**options)
                view=recovery_view(options['checkpoint_dir'],'alice')
                self.assertEqual(view['blocked_tasks'],['unsafe']);self.assertNotIn('private synthetic prompt',json.dumps(view))

    def test_model_reservations_survive_and_block_extra_calls(self):
        class Client:
            model='fixed';timeout=3
            calls=0
            def complete(self,*args):
                self.calls+=1
                return {'content':'unchanged','finish_reason':'stop','usage':{'prompt_tokens':1,'completion_tokens':1}}
        with tempfile.TemporaryDirectory() as tmp:
            client=Client();options=self.options(tmp,model_client=client,model_budget={'calls':1,'output_tokens':32})
            tasks=[{'id':'first','agent':'a','action':'llm','messages':[{'role':'user','content':'fixed'}],'max_tokens':16},
                   {'id':'pause','action':'sleep','seconds':.01,'depends_on':['first']},
                   {'id':'second','agent':'a','action':'llm','messages':[{'role':'user','content':'fixed'}],'max_tokens':16,'depends_on':['pause']}]
            self.interrupt(tasks,options,'pause');self.assertEqual(client.calls,1)
            states,_=run(tasks,max_workers=1,resume=True,**options)
            self.assertEqual(states['first'],'succeeded');self.assertEqual(states['second'],'failed');self.assertEqual(client.calls,1)
            snapshot=read_checkpoint(options['checkpoint_dir'],'alice')
            self.assertEqual(snapshot['model_ledger']['used'],{'calls':1,'output_tokens':16})
            summary=json.loads((Path(snapshot['root'])/'summary.json').read_text())
            acb=next(a for a in summary['agents'] if a['id']=='a')
            self.assertEqual(acb['model_calls'],1)
            self.assertEqual(acb['token_usage'],{'prompt_tokens':1,'completion_tokens':1})

    def test_identity_config_and_concurrent_lease_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            options=self.options(tmp);tasks=[{'id':'ok'}];run(tasks,**options)
            with self.assertRaises(PermissionError):read_checkpoint(options['checkpoint_dir'],'bob')
            with self.assertRaises(ValueError):run([{'id':'changed'}],resume=True,**options)
            data=read_checkpoint(options['checkpoint_dir'],'alice')
            first=WorkflowCheckpoint(options['checkpoint_dir'],'alice',data['specification'],resume=True)
            try:
                with self.assertRaises(BlockingIOError):WorkflowCheckpoint(options['checkpoint_dir'],'alice',data['specification'],resume=True)
            finally:first.close()

    def test_dynamic_children_survive_and_completed_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks=[{'id':'parent','spawn':[{'id':'child','action':'sleep','seconds':.01,'depends_on':['parent']},
                                        {'id':'bad','action':'fail','depends_on':['parent']}]}]
            options=self.options(tmp);self.interrupt(tasks,options,'child')
            states,_=run(tasks,max_workers=1,resume=True,**options)
            self.assertEqual(states,{'parent':'succeeded','child':'succeeded','bad':'failed'})
            again,_=run(tasks,max_workers=1,resume=True,**options);self.assertEqual(again,states)

    def test_checkpoint_symlink_and_corrupt_context_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks=[{'id':'done','message':'retained'},{'id':'pause','action':'sleep','depends_on':['done']}]
            options=self.options(tmp);self.interrupt(tasks,options,'pause')
            data=read_checkpoint(options['checkpoint_dir'],'alice');ref=data['outputs']['done']
            (Path(data['root'])/'contexts'/ref).write_text('"corrupted"')
            with self.assertRaises(ValueError):run(tasks,max_workers=1,resume=True,**options)
            actual=Path(options['checkpoint_dir'])/'state.json';backup=actual.with_name('backup');actual.rename(backup);actual.symlink_to(backup)
            with self.assertRaises(ValueError):read_checkpoint(options['checkpoint_dir'],'alice')

    def test_console_recovers_safe_demo_after_loading_interrupted_record(self):
        import threading,time,uuid
        from agent_runtime.web import Controller
        with tempfile.TemporaryDirectory() as tmp:
            controller=Controller(Path(tmp)/'console',user_identity='alice');key=uuid.uuid4().hex
            directory=controller.root/key
            tasks=[{'id':'done'},{'id':'pause','action':'sleep','seconds':.01,'depends_on':['done']}]
            options=dict(output_dir=directory,checkpoint_dir=directory/'recovery',recovery_owner='alice',quiet=True)
            self.interrupt(tasks,options,'pause')
            controller.store.save({'id':key,'preset':'dynamic','status':'running','created':time.time(),'workers':1,'policy':'resource_fit','budget':{},'history':[]})
            restored=Controller(controller.root,user_identity='alice')
            self.assertTrue(restored.detail(key)['recovery']['can_resume_demo'])
            restored.resume_safe_demo(key)
            end=time.monotonic()+5
            while restored.jobs[key]['status']=='running' and time.monotonic()<end:time.sleep(.01)
            self.assertEqual(restored.jobs[key]['status'],'succeeded')
            self.assertEqual(sum(e['task_id']=='done' and e['state']=='running' for e in restored.detail(key)['events']),1)
            with self.assertRaises(ValueError):restored.resume_safe_demo(key)

    def test_uncommitted_journal_tail_is_preserved_but_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            options=self.options(tmp);tasks=[{'id':'first'},{'id':'pause','action':'sleep','seconds':.01,'depends_on':['first']}]
            self.interrupt(tasks,options,'pause')
            data=read_checkpoint(options['checkpoint_dir'],'alice');root=Path(data['root'])
            with (root/'events.jsonl').open('a') as log:log.write('{"task_id":"uncommitted-tail","state":"succeeded"}\n')
            states,events=run(tasks,max_workers=1,resume=True,**options)
            self.assertTrue(all(v=='succeeded' for v in states.values()))
            self.assertNotIn('uncommitted-tail',(root/'events.jsonl').read_text())
            self.assertTrue(any('uncommitted-tail' in p.read_text() for p in root.glob('events-interrupted-*.jsonl')))
            self.assertEqual([e['sequence'] for e in events],list(range(1,len(events)+1)))

    def test_new_dynamic_agent_after_model_call_keeps_the_complete_ledger(self):
        class Client:
            model='fixed';timeout=3;calls=0
            def complete(self,*args):
                self.calls+=1;return {'content':'fixed','finish_reason':'stop','usage':{}}
        with tempfile.TemporaryDirectory() as tmp:
            client=Client();options=self.options(tmp,model_client=client)
            tasks=[{'id':'parent','action':'llm','messages':[{'role':'user','content':'fixed'}],
                    'spawn':[{'id':'child','agent':'new-agent','action':'sleep','seconds':.01,'depends_on':['parent']}]}]
            self.interrupt(tasks,options,'child')
            data=read_checkpoint(options['checkpoint_dir'],'alice')
            self.assertEqual(set(data['model_ledger']['agents']),{'parent','new-agent'})
            states,_=run(tasks,max_workers=1,resume=True,**options)
            self.assertTrue(all(v=='succeeded' for v in states.values()));self.assertEqual(client.calls,1)

    def test_restart_with_unconfirmed_old_scope_quarantines_new_admission(self):
        import time,uuid
        from agent_runtime.web import Controller
        with tempfile.TemporaryDirectory() as tmp:
            c=Controller(Path(tmp)/'console',user_identity='alice');key=uuid.uuid4().hex;directory=c.root/key
            options=dict(output_dir=directory,checkpoint_dir=directory/'recovery',recovery_owner='alice',quiet=True)
            tasks=[{'id':'pause','action':'sleep','seconds':.01}];self.interrupt(tasks,options,'pause')
            state=directory/'recovery/state.json';value=json.loads(state.read_text());value['clean_shutdown']=False;value['cgroup_root']=None;state.write_text(json.dumps(value))
            c.store.save({'id':key,'preset':'dynamic','status':'running','created':time.time(),'history':[]})
            recovered=Controller(c.root,user_identity='alice')
            self.assertTrue(recovered.recovery_required)
            self.assertEqual(recovered.jobs[key]['status'],'recovery_required')
            with self.assertRaises(ValueError):recovered.submit({'preset':'dynamic'})
