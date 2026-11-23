from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import time
import unittest

from runtime.launch import AIOSKernelRuntime


class NativeDAGConcurrencyTest(unittest.TestCase):
    def test_branches_overlap_and_join_uses_both_outputs(self):
        rendezvous = Barrier(2)

        def branch(task, upstream, attempt):
            rendezvous.wait(timeout=2)
            return {"value": 21}

        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            result = kernel.workflow_engine.run_workflow('parallel-test', [
                {'id': 'left', 'handler': branch}, {'id': 'right', 'handler': branch},
                {'id': 'join', 'depends_on': ['left', 'right'],
                 'handler': lambda task, upstream, attempt: {'sum': sum(item['value'] for item in upstream.values())}},
            ])
            self.assertEqual(result['results']['join'], {'sum': 42})
            self.assertTrue(all(state == 'succeeded' for state in result['states'].values()))
            self.assertEqual(kernel.scheduler.drf_controller.used.cpu_milli, 0)

    def test_resource_wait_recovers_and_failed_branch_is_isolated(self):
        entered, release = Event(), Event()

        def hold(*_):
            entered.set()
            release.wait(timeout=3)
            return {'ok': True}

        def fail(*_):
            raise ValueError('synthetic failure')

        with (TemporaryDirectory() as folder,
              AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel,
              ThreadPoolExecutor(max_workers=1) as pool):
            tasks = [
                {'id': 'hold', 'handler': hold, 'resources': {'cpu_milli': 4000}},
                {'id': 'healthy', 'handler': lambda *_: {'ok': True}},
                {'id': 'broken', 'handler': fail}, {'id': 'skip', 'depends_on': ['broken']},
            ]
            future = pool.submit(kernel.workflow_engine.run_workflow, 'resource-test', tasks)
            try:
                self.assertTrue(entered.wait(timeout=2))
                graph = kernel.workflow_engine.recent_graphs()[0]
                self.assertEqual(next(node['state'] for node in graph['nodes'] if node['id'] == 'healthy'), 'waiting')
                self.assertEqual(kernel.scheduler.drf_controller.used.cpu_milli, 4000)
            finally:
                release.set()
            result = future.result(timeout=5)
            self.assertEqual(result['states']['healthy'], 'succeeded')
            self.assertEqual(result['states']['skip'], 'isolated_skipped')
            self.assertEqual(kernel.scheduler.drf_controller.used.cpu_milli, 0)

    def test_paused_agent_waits_for_resume_and_times_out_explicitly(self):
        with (TemporaryDirectory() as folder,
              AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel,
              ThreadPoolExecutor(max_workers=1) as pool):
            drf = kernel.scheduler.drf_controller
            drf.pause_agent('paused')
            tasks = [{'id': 'paused', 'handler': lambda *_: {'ok': True}}]
            future = pool.submit(kernel.workflow_engine.run_workflow, 'resume-test', tasks)
            time.sleep(0.03)
            self.assertFalse(future.done())
            drf.resume_agent('paused')
            self.assertEqual(future.result(timeout=3)['states']['paused'], 'succeeded')
            drf.pause_agent('paused')
            with self.assertRaises(TimeoutError):
                kernel.workflow_engine.run_workflow('timeout-test', tasks, admission_timeout=0.03)


if __name__ == '__main__':
    unittest.main()
