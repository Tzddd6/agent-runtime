"""Behavioral scheduling scenarios with a deterministic clock; no real sleeps."""
import unittest
from agent_runtime.scheduling import AdmissionScheduler


class SchedulingTests(unittest.TestCase):
    def scheduler(self, policy='aging_fit', **kwargs):
        return AdmissionScheduler(policy, {'cpu':2,'memory':100,'pids':10},
                                  {'cpu':1,'memory':10,'pids':1}, **kwargs)

    def test_replenished_small_jobs_cannot_keep_bypassing_aged_wide_job(self):
        wide = {'id':'wide','resources':{'cpu':2}}
        used = {'cpu':1,'memory':10,'pids':1}
        baseline, fair = self.scheduler('resource_fit'), self.scheduler(aging_seconds=2)
        # One old running task occupies half the machine; new short jobs keep
        # arriving whenever the other half becomes free.
        for instant in (0,1):
            ready = [wide, {'id':f'small-{instant}'}]
            self.assertEqual(baseline.choose(ready,used,now=instant).task_id,f'small-{instant}')
            self.assertEqual(fair.choose(ready,used,now=instant).task_id,f'small-{instant}')
        for instant in (2,3,4):
            ready = [wide, {'id':f'small-{instant}'}]
            self.assertEqual(baseline.choose(ready,used,now=instant).task_id,f'small-{instant}')
            result = fair.choose(ready,used,now=instant)
            self.assertIsNone(result.task_id)
            self.assertEqual(result.draining_task_id,'wide')
            self.assertEqual(result.reason,'aging_drain')
        # Once the preexisting worker finishes, the wide task wins even though
        # fitting small work continues arriving. No running task was interrupted.
        free = {'cpu':0,'memory':0,'pids':0}
        result = fair.choose([wide,{'id':'new-small'}],free,now=5)
        self.assertEqual(result.task_id,'wide')
        self.assertEqual(result.reason,'aged_admission')

    def test_multidimensional_memory_pressure_also_drains(self):
        scheduler = self.scheduler(aging_seconds=1)
        ready = [{'id':'memory-heavy','resources':{'cpu':1,'memory':90}}, {'id':'light'}]
        used = {'cpu':1,'memory':30,'pids':1}
        self.assertEqual(scheduler.choose(ready,used,now=0).task_id,'light')
        self.assertEqual(scheduler.choose(ready,used,now=1).reason,'aging_drain')
        self.assertEqual(scheduler.choose(ready,{'cpu':0,'memory':0,'pids':0},now=2).task_id,'memory-heavy')

    def test_busy_agent_and_impossible_task_cannot_block_unrelated_work(self):
        scheduler = self.scheduler(aging_seconds=1)
        ready = [{'id':'impossible','resources':{'memory':101}},
                 {'id':'busy-old','agent':'serial'}, {'id':'independent'}]
        used = {'cpu':1,'memory':10,'pids':1}
        scheduler.choose(ready,used,now=0,busy_agents=['serial'])
        result = scheduler.choose(ready,used,now=3,busy_agents=['serial'])
        self.assertEqual(result.task_id,'independent')
        self.assertIsNone(result.draining_task_id)

    def test_readiness_and_retry_reset_wait_clock(self):
        scheduler = self.scheduler(aging_seconds=1)
        occupied = {'cpu':1,'memory':10,'pids':1}
        wide = {'id':'wide','resources':{'cpu':2}}
        scheduler.choose([wide],occupied,now=0)
        self.assertEqual(scheduler.choose([wide],occupied,now=2).reason,'aging_drain')
        scheduler.choose([],occupied,now=3)
        # A task not continuously ready (e.g. retry re-created after launch)
        # does not carry stale age across a different Attempt.
        result = scheduler.choose([wide,{'id':'small'}],occupied,now=4)
        self.assertEqual(result.task_id,'small')

    def test_fifo_baseline_and_resource_fit_have_different_head_behavior(self):
        ready = [{'id':'wide','resources':{'cpu':2}}, {'id':'small'}]
        used = {'cpu':1,'memory':10,'pids':1}
        self.assertEqual(self.scheduler('fifo').choose(ready,used,now=0).reason,'fifo_head_blocked')
        self.assertEqual(self.scheduler('resource_fit').choose(ready,used,now=0).task_id,'small')

    def test_immediate_retry_does_not_inherit_completed_attempt_age(self):
        scheduler = self.scheduler(aging_seconds=1)
        wide = {'id':'retrying','resources':{'cpu':2}}
        free = {'cpu':0,'memory':0,'pids':0}
        occupied = {'cpu':1,'memory':10,'pids':1}
        scheduler.choose([wide],free,now=0)
        self.assertEqual(scheduler.choose([wide],free,now=2).reason,'aged_admission')
        scheduler.started('retrying')
        # The Attempt exits before choose runs again, so the ID appears in
        # consecutive ready sets. started() is what resets its retry's age.
        result = scheduler.choose([wide,{'id':'other'}],occupied,now=2.1)
        self.assertEqual(result.task_id,'other')
        self.assertIsNone(result.draining_task_id)
        self.assertEqual(scheduler.choose([wide],occupied,now=3.2).reason,'aging_drain')
        scheduler.started('unknown')

    def test_equal_age_preserves_arrival_order_and_checks_slot_limit(self):
        scheduler = self.scheduler(aging_seconds=1)
        ready = [{'id':'first'},{'id':'second'}]
        free = {'cpu':0,'memory':0,'pids':0}
        self.assertIsNone(scheduler.choose(ready,free,now=0,slots_available=0).task_id)
        # Input list reordering does not change the original arrival tie-break.
        self.assertEqual(scheduler.choose(list(reversed(ready)),free,now=2).task_id,'first')

    def test_rejects_bad_resource_accounting_and_aging_parameters(self):
        for threshold in (0,-1,float('nan'),float('inf'),True):
            with self.assertRaises(ValueError):self.scheduler(aging_seconds=threshold)
        with self.assertRaises(ValueError):
            self.scheduler().choose([{'id':'x'}],{'cpu':3,'memory':0,'pids':0},now=0)


if __name__ == '__main__':
    unittest.main()
