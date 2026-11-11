import collections
import json
from pathlib import Path
import tempfile
import unittest
from tools.benchmark_projects import BUDGET,Console,StopBenchmark,run,schedule


class BenchmarkProtocolTests(unittest.TestCase):
    def test_balanced_unique_design(self):
        rows=schedule()
        self.assertEqual(len(rows),54)
        counts=collections.Counter((r['project'],r['workers'],r['policy']) for r in rows)
        self.assertEqual(len(counts),18)
        self.assertEqual(set(counts.values()),{3})
        self.assertEqual(rows,schedule())

    def test_post_intent_survives_lost_ack_and_resume_never_resubmits(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            class Fake:
                posts=0
                def available(self):return {'jobs':[],'csrf':'fixture'}
                def request(self,*args):
                    self.posts+=1
                    checkpoint=json.loads((root/'checkpoint.json').read_text())
                    assert checkpoint['runs']['0']['phase']=='post_intent'
                    assert checkpoint['reserved_call_upper_bound']==4
                    raise OSError('lost acknowledgement')
            fake=Fake()
            with self.assertRaisesRegex(StopBenchmark,'acknowledgement uncertain'):run(root,fake)
            with self.assertRaisesRegex(StopBenchmark,'Unacknowledged POST intent'):run(root,fake)
            self.assertEqual(fake.posts,1)

    def test_known_job_resume_only_gets_and_evidence_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            class Fake:
                posts=0
                def available(self):return {'jobs':[],'csrf':'fixture'}
                def request(self,*args):self.posts+=1;return {'id':'fixture1'}
                def get(self,path):
                    return {'id':'fixture1','status':'cancelled','created':0,'budget':BUDGET,
                            'events':[],'history':[],'engineering':{}}
            fake=Fake()
            with self.assertRaisesRegex(StopBenchmark,'Infrastructure'):run(root,fake)
            self.assertTrue((root/'runs'/'000'/'detail.json').exists())
            checkpoint=json.loads((root/'checkpoint.json').read_text())
            checkpoint['runs']['0']['phase']='polling'
            checkpoint.pop('halt_reason')
            (root/'checkpoint.json').write_text(json.dumps(checkpoint))
            with self.assertRaisesRegex(StopBenchmark,'Infrastructure'):run(root,fake)
            self.assertEqual(fake.posts,1)

    def test_status_budget_missing_or_changed_rejected_before_post(self):
        console=Console('fixture-private-control-token')
        status={'model_configured':True,'model_available':True,'fixed_budget':True,
                'enforcement':'cgroup_v2','jobs':[]}
        console.get=lambda path:status
        with self.assertRaises(StopBenchmark):console.available()
        status['budget']=dict(BUDGET);self.assertEqual(console.available(),status)
        status['budget']['cpu_milli']=4000
        with self.assertRaises(StopBenchmark):console.available()


if __name__=='__main__':unittest.main()
