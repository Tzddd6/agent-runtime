import json,os,signal,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agent_runtime.runtime import run
from agent_runtime.model_gateway import ModelGateway
from agent_runtime.worker_session import WorkerSession


@unittest.skipUnless(sys.platform=='linux','requires Linux credential-free forkserver')
class SessionTests(unittest.TestCase):
    def test_preloader_dies_during_model_call_no_replay_then_next_workflow_recovers(self):
        with tempfile.TemporaryDirectory() as folder,WorkerSession(folder) as session:
            class Client:
                model='fixed';timeout=5
                calls=0
                def complete(self,messages,max_tokens):
                    import time
                    self.calls+=1
                    session.preloader.process.kill();session.preloader.process.wait(timeout=3)
                    time.sleep(.05)
                    return {'content':'not safely delivered','finish_reason':'stop','usage':{'prompt_tokens':1,'completion_tokens':1}}
            client=Client()
            with patch.object(ModelGateway,'from_env',side_effect=lambda log:ModelGateway(client,log)):
                states,_=run([{'id':'llm','action':'llm','messages':[{'role':'user','content':'fixed'}]}],quiet=True,
                    output_dir=folder,worker_session=session,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertEqual(states['llm'],'failed');self.assertEqual(client.calls,1)
            states,_=run([{'id':'after','message':'safe new workflow'}],quiet=True,output_dir=folder,
                worker_session=session,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertEqual(states['after'],'succeeded');self.assertEqual(session.stats['preloader_starts'],2)

    def test_timeout_does_not_poison_next_attempt(self):
        with tempfile.TemporaryDirectory() as folder,WorkerSession(folder) as session:
            states,_=run([{'id':'timeout','action':'sleep','seconds':2,'timeout':.3}],quiet=True,output_dir=folder,
                worker_session=session,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertNotEqual(states['timeout'],'succeeded')
            states,_=run([{'id':'after'}],quiet=True,output_dir=folder,worker_session=session,
                cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertEqual(states['after'],'succeeded')

    @unittest.skipUnless(os.environ.get('AGENTRT_TEST_CGROUP'),'requires real delegated cgroup')
    def test_oom_reclaims_group_and_session_accepts_new_attempt(self):
        with tempfile.TemporaryDirectory() as folder,WorkerSession(folder) as session:
            group=os.environ['AGENTRT_TEST_CGROUP']
            states,events=run([{'id':'oom','action':'load','memory_mib':64,'seconds':1,
                'resources':{'memory_bytes':32*1024**2}}],quiet=True,output_dir=folder,worker_session=session,cgroup_root=group)
            self.assertEqual(states['oom'],'failed')
            failure=next(e for e in events if e['state']=='failed')
            self.assertGreater(int(dict(line.split() for line in failure['usage']['memory.events'].splitlines())['oom_kill']),0)
            self.assertFalse(any(p.is_dir() for p in Path(group).iterdir()))
            states,_=run([{'id':'after'}],quiet=True,output_dir=folder,worker_session=session,cgroup_root=group)
            self.assertEqual(states['after'],'succeeded')

    def test_fresh_attempts_reuse_preloader_and_recover_between_workflows(self):
        with tempfile.TemporaryDirectory() as folder,WorkerSession(folder) as session:
            def execute():
                states,events=run([{'id':'echo','message':'unchanged'}],quiet=True,output_dir=folder,worker_session=session,
                    cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
                self.assertEqual(states,{'echo':'succeeded'})
                self.assertEqual(events[-1]['result']['result'],'unchanged')
                return next(e['pid'] for e in events if e['state']=='running')
            first=execute();second=execute()
            self.assertNotEqual(first,second);self.assertEqual(session.stats['preloader_starts'],1)
            os.kill(session.preloader.process.pid,signal.SIGKILL);session.preloader.process.wait(timeout=3)
            execute();self.assertEqual(session.stats['preloader_starts'],2)
            self.assertEqual(session.stats['recovered_preloaders'],1)

    def test_lease_rejects_concurrent_or_outside_workflow(self):
        with tempfile.TemporaryDirectory() as folder,WorkerSession(folder) as session:
            with self.assertRaises(ValueError):session.acquire(Path(folder).parent/'outside')
            session.acquire(folder)
            try:
                with self.assertRaises(RuntimeError):session.acquire(folder)
                with self.assertRaises(RuntimeError):session.close()
            finally:session.release()

    def test_empty_model_context_has_no_ipc_and_exact_messages(self):
        class Client:
            model='fixed';timeout=5
            def __init__(self):self.messages=[]
            def complete(self,messages,max_tokens):
                self.messages.append(messages)
                return {'content':'exact answer','finish_reason':'stop','usage':{'prompt_tokens':1,'completion_tokens':1}}
        with tempfile.TemporaryDirectory() as folder,WorkerSession(folder) as session:
            client=Client();messages=[{'role':'user','content':'preserve this'}]
            with patch.object(ModelGateway,'from_env',side_effect=lambda log:ModelGateway(client,log)),patch('agent_runtime.runtime.Broker',side_effect=AssertionError('Empty context must not open IPC server')):
                for _ in range(2):
                    states,_=run([{'id':'llm','action':'llm','messages':messages}],quiet=True,output_dir=folder,
                        worker_session=session,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
                    self.assertEqual(states['llm'],'succeeded')
            self.assertEqual(client.messages,[messages,messages])


if __name__=='__main__':unittest.main()
