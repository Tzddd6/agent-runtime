from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import unittest
from unittest.mock import patch

from aios.scheduler.drf_admission import DRFAdmissionController, ResourceVector
from aios.scheduler.workflow_dispatch import ReadyNode, WorkflowDispatchQueue, remaining_paths
from runtime.launch import AIOSKernelRuntime


class SharedWorkflowDispatchTest(unittest.TestCase):
    def test_paths_ignore_completed_nodes_and_count_join_only_once_per_path(self):
        tasks = {'root': {'estimate_seconds': .1}, 'fast': {'estimate_seconds': .2, 'depends_on': ['root']},
                 'slow': {'estimate_seconds': .3, 'depends_on': ['root']},
                 'join': {'estimate_seconds': .4, 'depends_on': ['fast', 'slow']}}
        paths = remaining_paths(tasks, dict.fromkeys(tasks, 'waiting'))
        self.assertAlmostEqual(paths['root'], .8)
        self.assertAlmostEqual(paths['fast'], .6)
        after = remaining_paths(tasks, {**dict.fromkeys(tasks, 'waiting'), 'fast': 'succeeded'})
        self.assertNotIn('fast', after)
        self.assertAlmostEqual(after['root'], .8)

    def test_oldest_admissible_work_overtakes_urgent_work_after_aging(self):
        admission = DRFAdmissionController()
        queue = WorkflowDispatchQueue(admission, 'critical_path', slots=1, aging_seconds=.5)
        res = ResourceVector(100, 1024, 1)
        older = ReadyNode('tests', 'old-agent', res, True, .2, 20)
        urgent = ReadyNode('answer', 'short-agent', res, True, .05, 10.6)
        with patch('aios.scheduler.workflow_dispatch.monotonic', return_value=10.0):
            queue.publish('old', [older], True)
        with patch('aios.scheduler.workflow_dispatch.monotonic', return_value=10.51):
            queue.publish('urgent', [urgent], True)
            self.assertIsNone(queue.claim('urgent', timeout=0))
            self.assertEqual(queue.claim('old', timeout=0).evidence['reason'], 'aging')
        self.assertIsNone(queue.claim('urgent', timeout=0))
        admission.release('old-agent', res)
        queue.remove('old')
        queue.remove('urgent')
        self.assertFalse(any(admission.snapshot()['used'].values()))

    def test_two_dags_compete_for_the_same_slot_and_critical_path_goes_first(self):
        with TemporaryDirectory() as path, AIOSKernelRuntime(storage_root=str(Path(path) / 'store')) as kernel:
            engine = kernel.workflow_engine
            available = {'blocker': Event(), 'long': Event(), 'urgent': Event()}
            release = Event()
            order = []

            class AnnouncedQueue(WorkflowDispatchQueue):
                def publish(self, workflow, nodes, can_dispatch, completed=()):
                    super().publish(workflow, nodes, can_dispatch, completed)
                    if nodes:
                        available[workflow].set()

            queue = AnnouncedQueue(kernel.scheduler.drf_controller, 'critical_path', slots=1, aging_seconds=2)

            def blocker(*_):
                if not release.wait(5):
                    raise TimeoutError('Test gate was not released')
                return {'ok': True}

            def work(task, _upstream, _attempt):
                order.append(task['id'])
                return {'ran': task['id']}

            def run(workflow, tasks, deadline):
                return engine.run_workflow(workflow, tasks, dispatch_queue=queue, deadline_at=deadline)

            now = monotonic()
            with ThreadPoolExecutor(max_workers=3) as pool:
                try:
                    hold = pool.submit(run, 'blocker', [{'id': 'hold', 'handler': blocker}], now + 1)
                    self.assertTrue(available['blocker'].wait(5))
                    long = pool.submit(run, 'long', [
                        {'id': 'other-tests', 'handler': work, 'estimate_seconds': .2},
                    ], now + 20)
                    urgent = pool.submit(run, 'urgent', [
                        {'id': 'side', 'handler': work, 'estimate_seconds': .02},
                        {'id': 'root', 'handler': work, 'estimate_seconds': .1},
                        {'id': 'tail', 'handler': work, 'estimate_seconds': .2, 'depends_on': ['root']},
                    ], now + 2)
                    self.assertTrue(available['long'].wait(5))
                    self.assertTrue(available['urgent'].wait(5))
                finally:
                    release.set()
                hold.result(timeout=5)
                urgent_result = urgent.result(timeout=5)
                long.result(timeout=5)

            self.assertEqual(order, ['root', 'tail', 'side', 'other-tests'])
            self.assertEqual(queue.peak_active, 1)
            started = next(row for row in urgent_result['events'] if row['state'] == 'running')
            self.assertAlmostEqual(started['dispatch']['path_ms'], 300)
            self.assertFalse(any(kernel.scheduler.drf_controller.snapshot()['used'].values()))

    def test_context_error_releases_reserved_resources_and_shared_slot(self):
        with TemporaryDirectory() as path, AIOSKernelRuntime(storage_root=str(Path(path) / 'store')) as kernel:
            queue = WorkflowDispatchQueue(kernel.scheduler.drf_controller, 'fifo', slots=1)
            tasks = [{'id': 'root', 'handler': lambda *_: {}},
                     {'id': 'tail', 'depends_on': ['root'], 'handler': lambda *_: {}}]
            with patch.object(kernel.context_store, 'build_budget_view', side_effect=RuntimeError('context failed')):
                with self.assertRaisesRegex(RuntimeError, 'context failed'):
                    kernel.workflow_engine.run_workflow('failure', tasks, dispatch_queue=queue)
            self.assertFalse(any(kernel.scheduler.drf_controller.snapshot()['used'].values()))
            result = kernel.workflow_engine.run_workflow('next', tasks, dispatch_queue=queue)
            self.assertEqual(result['states'], {'root': 'succeeded', 'tail': 'succeeded'})
