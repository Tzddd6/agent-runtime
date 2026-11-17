import json,os,signal,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agent_runtime.runtime import run
from agent_runtime.model_gateway import ModelGateway
from agent_runtime.worker_session import WorkerSession


@unittest.skipUnless(sys.platform=='linux','requires Linux credential-free forkserver')
class SessionTests(unittest.TestCase):
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
