import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from agent_runtime.completion import CompletionWaiter
from agent_runtime.runtime import run
from agent_runtime.execution_view import execution_view


class ExecutionSpeedTests(unittest.TestCase):
    def test_dependency_chain_dispatches_after_cleanup_and_records_scheduler_stats(self):
        tasks=[{'id':f't{i}','depends_on':[f't{i-1}'] if i else []} for i in reversed(range(8))]
        with tempfile.TemporaryDirectory() as tmp:
            states,events=run(tasks,output_dir=tmp,quiet=True,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue(all(s=='succeeded' for s in states.values()))
            self.assertEqual([e['task_id'] for e in events if e['state']=='running'],[f't{i}' for i in range(8)])
            stats=json.loads(next(Path(tmp).glob('*/summary.json')).read_text())['scheduling_stats']
            self.assertGreater(stats['decisions'],0)
            self.assertGreater(stats['iterations'],0)

    def test_notification_failure_falls_back_and_closes_resources(self):
        waiter=CompletionWaiter()
        try:
            with patch('agent_runtime.completion.sys.platform','linux'),patch('agent_runtime.completion.os.pidfd_open',side_effect=OSError('unsupported'),create=True):
                waiter.add('a',type('Proc',(),{'pid':1})())
            with patch('agent_runtime.completion.time.sleep') as sleep:
                waiter.wait(.02);sleep.assert_called_once_with(.02)
            self.assertEqual(waiter.stats['fallback_registrations'],1)
            self.assertFalse(waiter.files)
        finally:waiter.close()

    @unittest.skipUnless(sys.platform=='linux' and hasattr(os,'pidfd_open'),'Linux pidfd required')
    def test_real_exit_wakes_selector_and_pidfd_is_closed(self):
        waiter=CompletionWaiter();proc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.05)'])
        try:
            waiter.add('p',proc);fd=waiter.files['p'];waiter.wait(3);proc.wait(timeout=3)
            self.assertEqual(waiter.stats['exit_wakeups'],1)
            waiter.remove('p');waiter.remove('p')
            with self.assertRaises(OSError):os.fstat(fd)
        finally:
            if proc.poll() is None:proc.kill();proc.wait()
            waiter.close()

    def test_execution_trace_is_metadata_only_and_tracks_retry_attempts(self):
        job={'preset':'review','status':'succeeded','created':10,'finished':15,'events':[
            {'task_id':'writer-1','state':'running','attempt':1,'time':11},
            {'task_id':'writer-1','state':'waiting','attempt':1,'time':12,'result':{'secret':'DO NOT EXPORT'}},
            {'task_id':'writer-1','state':'running','attempt':2,'time':12},
            {'task_id':'writer-1','state':'succeeded','attempt':2,'time':14}],
             'model_resources':{'used':{'calls':2}}}
        view=execution_view(job)
        self.assertEqual(view['elapsed_seconds'],5)
        self.assertEqual([s['elapsed_seconds'] for s in view['steps']],[1,2])
        self.assertEqual([s['status'] for s in view['steps']],['retrying','succeeded'])
        self.assertEqual(view['model_admitted'],2)
        self.assertNotIn('DO NOT EXPORT',json.dumps(view))
        self.assertFalse(view['thinking_content'])

    def test_finished_workflow_never_reports_a_step_as_still_running(self):
        job={'preset':'llm','status':'interrupted','created':10,'finished':12,
             'events':[{'task_id':'assistant','state':'running','attempt':1,'time':11}]}
        self.assertEqual(execution_view(job)['steps'][0]['status'],'interrupted')

    def test_idle_metrics_cache_never_skips_activity_transitions(self):
        from agent_runtime.agents import AgentRegistry
        with tempfile.TemporaryDirectory() as tmp:
            registry=AgentRegistry({'cpu_milli':1000,'memory_bytes':1024**3,'pids':32})
            acb=registry.register('a');registry.groups['a']=Path(tmp)
            with patch.object(registry.sampler,'sample',return_value={'memory_bytes':7}) as sample,\
                 patch('agent_runtime.agents.time.monotonic',return_value=10) as clock:
                registry.snapshots();registry.snapshots();self.assertEqual(sample.call_count,1)
                acb.active['work']=42
                registry.snapshots();registry.snapshots();self.assertEqual(sample.call_count,3)
                acb.active.clear();registry.snapshots();self.assertEqual(sample.call_count,4)
                registry.snapshots();self.assertEqual(sample.call_count,4)
                clock.return_value=10.5;registry.snapshots();self.assertEqual(sample.call_count,5)
