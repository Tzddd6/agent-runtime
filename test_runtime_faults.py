"""Control-plane failure boundaries and deadlines, beyond ordinary Worker exits."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from agent_runtime.context import ContextStore
from agent_runtime.runtime import run


class RuntimeFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def execute(self,tasks,**kwargs):
        return run(tasks,output_dir=self.temp.name,quiet=True,**kwargs)

    def test_oversized_context_does_not_abort_independent_running_task(self):
        states,events=self.execute([
            {'id':'independent','action':'sleep','seconds':.15},
            {'id':'bad','contexts':[{'text':'x'*(1024**2+1)}]},
            {'id':'dependent','depends_on':['bad']}])
        self.assertEqual(states,{'independent':'succeeded','bad':'failed','dependent':'skipped'})
        self.assertIn('输入准备失败',next(e['error'] for e in events if e['task_id']=='bad'))

    def test_context_read_error_remains_task_local(self):
        original=ContextStore.get
        def read(store,ref,agent):
            if agent=='bad':raise PermissionError('fixture read denied')
            return original(store,ref,agent)
        with patch.object(ContextStore,'get',read):
            states,_=self.execute([{'id':'bad','contexts':[{'data':'shared'}]},
                                   {'id':'good','contexts':[{'data':'shared'}]}])
        self.assertEqual(states,{'bad':'failed','good':'succeeded'})

    def test_non_json_python_configuration_does_not_kill_workflow(self):
        states,_=self.execute([{'id':'bad','message':object()},{'id':'good'}])
        self.assertEqual(states,{'bad':'failed','good':'succeeded'})

    def test_payload_file_limit_is_task_local(self):
        from agent_runtime.executor import Executor
        original=Executor.launch
        def launch(executor,payload,directory,limits):
            if payload['id']=='bad':raise ValueError('fixture payload too large')
            return original(executor,payload,directory,limits)
        with patch.object(Executor,'launch',launch):
            states,_=self.execute([{'id':'bad'},{'id':'good'}])
        self.assertEqual(states,{'bad':'failed','good':'succeeded'})

    def test_delegation_launch_failure_still_fails_closed(self):
        class Backend:
            def __init__(self,*args):pass
            def launch(self,*args):raise PermissionError('fixture cgroup permission denied')
        with patch('agent_runtime.runtime.Executor',Backend):
            with self.assertRaisesRegex(RuntimeError,'EXECUTION_BACKEND_FAILED'):
                self.execute([{'id':'task'}],cgroup_root='/sys/fs/cgroup/fixture')

    def test_cleanup_failure_attempts_other_handles_and_retains_failed_reservation(self):
        calls=[]
        samples=[]
        class Process:
            def __init__(self,key):self.key=key;self.pid=101 if key=='first' else 102
            def poll(self):return 0 if self.key=='first' else None
        class Backend:
            def __init__(self,*args):pass
            def launch(self,payload,directory,limits):
                return {'proc':Process(payload['id']),'directory':directory,'group':None,
                        'started':time.monotonic(),'key':payload['id']}
            def cleanup(self,handle):
                calls.append(handle['key'])
                if handle['key']=='first':raise OSError('fixture group still populated')
                return 0,'',{}
        with patch('agent_runtime.runtime.Executor',Backend):
            with self.assertRaisesRegex(RuntimeError,'CLEANUP_FAILED'):
                self.execute([{'id':'first'},{'id':'other'}],observer=samples.append)
        self.assertIn('other',calls)
        self.assertEqual(samples[-1]['reserved']['cpu_milli'],1000)
        self.assertEqual(samples[-1]['slots_used'],1)

    def test_blocked_observer_never_blocks_timeout_or_run_return(self):
        entered,release=threading.Event(),threading.Event()
        def blocked(sample):
            if any(task['state']=='running' for task in sample['tasks']):
                entered.set()
                release.wait(3)
        started=time.monotonic()
        try:
            states,_=self.execute([{'id':'slow','action':'sleep','seconds':2,'timeout':.12}],
                                   observer=blocked)
            elapsed=time.monotonic()-started
            self.assertTrue(entered.is_set())
            self.assertEqual(states['slow'],'timeout')
            self.assertLess(elapsed,1.5)
        finally:
            release.set()

    def test_expired_watchdog_flag_wins_even_after_process_has_exited(self):
        class Process:
            pid=101
            def poll(self):return 0
        class Backend:
            def __init__(self,*args):pass
            def launch(self,payload,directory,limits):
                self.timeout=payload['timeout']
                flag=threading.Event();flag.set()
                return {'proc':Process(),'directory':directory,'group':None,
                        'started':time.monotonic(),'expired':flag}
            def cleanup(self,handle):return 0,'',{}
        with patch('agent_runtime.runtime.Executor',Backend):
            states,_=self.execute([{'id':'late'}],timeout=.1)
        self.assertEqual(states['late'],'timeout')

    def test_normal_observer_final_snapshot_matches_journal_watermark(self):
        samples=[]
        states,events=self.execute([{'id':'first'},{'id':'second','depends_on':['first']}],
                                   observer=samples.append)
        self.assertTrue(all(state=='succeeded' for state in states.values()))
        self.assertEqual([e['sequence'] for e in events],list(range(1,len(events)+1)))
        self.assertEqual(samples[-1]['event_sequence'],len(events))
        self.assertEqual(samples[-1]['reserved'],{'cpu_milli':0,'memory_bytes':0,'pids':0})
        self.assertTrue(all(row['state']=='succeeded' for row in samples[-1]['tasks']))
        self.assertEqual([s['sample_sequence'] for s in samples],
                         sorted(s['sample_sequence'] for s in samples))


if __name__=='__main__':
    unittest.main()
