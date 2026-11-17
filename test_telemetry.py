import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
import unittest
from agent_runtime.runtime import run
from agent_runtime.telemetry import CgroupSampler


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_dependency_visibility_reservations_and_release(self):
        samples=[]
        tasks=[{'id':'first','action':'sleep','seconds':.1},
               {'id':'next','depends_on':['first']}]
        states,_=run(tasks,output_dir=self.root,observer=samples.append,quiet=True)
        self.assertEqual(states,{'first':'succeeded','next':'succeeded'})
        self.assertEqual(samples[0]['tasks'][1]['wait_reason'],'等待依赖任务')
        self.assertTrue(any(s['reserved']['cpu_milli']==1000 for s in samples))
        self.assertTrue(any(s['slots_used']==1 and any(t['pid'] for t in s['tasks']) for s in samples))
        self.assertEqual(samples[-1]['reserved'],{'cpu_milli':0,'memory_bytes':0,'pids':0})
        self.assertEqual(samples[-1]['slots_used'],0)
        self.assertEqual(samples[-1]['tasks'][-1]['state'],'succeeded')

    def test_observer_failure_does_not_fail_workflow(self):
        def broken(_):raise RuntimeError('monitor unavailable')
        states,_=run([{'id':'task'}],output_dir=self.root,observer=broken,quiet=True)
        self.assertEqual(states,{'task':'succeeded'})

    def test_observer_cannot_mutate_task_dependencies(self):
        def alter(sample):
            for task in sample['tasks']:task['depends_on'].append('not-a-real-task')
        states,_=run([{'id':'task','depends_on':[]}],output_dir=self.root,observer=alter,quiet=True)
        self.assertEqual(states,{'task':'succeeded'})

    def test_unavailable_measurements_are_not_zero_usage(self):
        sample=CgroupSampler().sample(None)
        self.assertEqual(sample['source'],'unavailable')
        self.assertIsNone(sample['cpu_cores'])
        self.assertIsNone(sample['memory_bytes'])

    @unittest.skipUnless(sys.platform=='linux' and os.environ.get('AGENTRT_TEST_CGROUP'),'需要 Linux 委派 cgroup')
    def test_linux_real_measurements(self):
        samples=[]
        states,_=run([{'id':'load','action':'load','seconds':2,'memory_mib':16,
                       'resources':{'cpu_milli':200,'memory_bytes':128*1024**2}}],
                     cgroup_root=os.environ['AGENTRT_TEST_CGROUP'],output_dir=self.root,
                     observer=samples.append,quiet=True)
        self.assertEqual(states,{'load':'succeeded'})
        metrics=[t['metrics'] for s in samples for t in s['tasks'] if t['state']=='running']
        self.assertTrue(any((m.get('memory_bytes') or 0)>16*1024**2 for m in metrics))
        cores=[m['cpu_cores'] for m in metrics if m.get('cpu_cores') is not None]
        self.assertGreater(statistics.median(cores),.05)
        self.assertLess(statistics.median(cores),.4)
        self.assertEqual(samples[-1]['reserved']['cpu_milli'],0)


if __name__=='__main__':unittest.main()
