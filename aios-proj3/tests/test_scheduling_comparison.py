from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import unittest
from unittest.mock import patch

from runtime.launch import AIOSKernelRuntime
from runtime.scheduling_comparison import compare_scheduling
from runtime.web import JobBusy, NativeConsole


class SchedulingComparisonTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))

    def test_fifo_keeps_older_ready_nodes_ahead_of_newly_unblocked_nodes(self):
        with AIOSKernelRuntime(storage_root=str(self.folder / 'storage')) as kernel:
            tasks = [
                {'id': 'child', 'depends_on': ['first'], 'handler': lambda *_: {}},
                {'id': 'first', 'handler': lambda *_: {}},
                {'id': 'second', 'handler': lambda *_: {}},
            ]
            result = kernel.workflow_engine.run_workflow('fifo-ready', tasks, max_workers=1, ready_policy='fifo')
        self.assertEqual([event['task_id'] for event in result['events'] if event['state'] == 'running'],
                         ['first', 'second', 'child'])

    def test_comparison_runs_equal_work_and_releases_all_reservations(self):
        result = compare_scheduling()
        self.assertTrue(result['verified'])
        self.assertEqual([trial['policy'] for trial in result['trials']], ['fifo', 'drf', 'drf', 'fifo'])
        for trial in result['trials']:
            self.assertEqual(trial['long_completed'], 6)
            self.assertEqual(trial['short_completed'], 3)
            self.assertEqual(trial['used_after'], {'cpu_milli': 0, 'memory_bytes': 0, 'pids': 0})
            self.assertEqual(len(trial['long_completion_ms']), 6)
            self.assertTrue(all(event['finish_ms'] >= event['wait_ms'] >= 0 for event in trial['timeline']))

    def test_two_offline_jobs_run_together_without_evicting_active_history(self):
        with AIOSKernelRuntime(storage_root=str(self.folder / 'storage')) as kernel:
            console = NativeConsole(kernel, self.folder / 'jobs')
            entered = Barrier(3)
            release = Event()
            original = console.run_scenario

            def hold_and_run(*args):
                entered.wait(timeout=5)
                if not release.wait(timeout=5):
                    raise TimeoutError('Concurrent jobs did not receive release')
                return original(*args)

            try:
                with patch.object(console, 'run_scenario', side_effect=hold_and_run):
                    first = console.submit({'preset': 'document', 'prompt': 'alpha beta'})['id']
                    for index in range(19):
                        identity = f'old{index}'
                        console.jobs[identity] = {'id': identity, 'status': 'succeeded'}
                    second = console.submit({'preset': 'document', 'prompt': 'gamma delta epsilon'})['id']
                    entered.wait(timeout=5)
                    self.assertEqual(len(console.jobs), 20)
                    self.assertNotIn('old0', console.jobs)
                    for identity in (first, second):
                        self.assertEqual(console.detail(identity)['status'], 'running')
                    with self.assertRaises(JobBusy):
                        console.submit({'preset': 'resources'})
                    with self.assertRaises(JobBusy):
                        console.submit({'preset': 'scheduling'})
                    release.set()
            finally:
                release.set()
                console.close()

            for identity in (first, second):
                result = console.detail(identity)
                self.assertEqual(result['status'], 'succeeded')
                self.assertEqual({event['agent'] for event in result['events']},
                                 {identity + '-stats', identity + '-report'})
            self.assertNotEqual(console.detail(first)['results']['stats'],
                                console.detail(second)['results']['stats'])
