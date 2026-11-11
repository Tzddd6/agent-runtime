import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from agent_runtime.context import ContextStore
from agent_runtime.runtime import run


class ExtendedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def execute(self, tasks, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return run(tasks, output_dir=self.temp.name, **kwargs)

    def test_dynamic_batch_and_retry(self):
        tasks = json.loads(Path('examples/dynamic.json').read_text(encoding='utf-8'))
        states, events = self.execute(tasks)
        self.assertTrue(all(s == 'succeeded' for s in states.values()))
        starts = [e for e in events if e['task_id'] == 'prepare_tests' and e['state'] == 'running']
        self.assertEqual([e['attempt'] for e in starts], [1, 2])
        review_start = next(i for i,e in enumerate(events) if e['task_id']=='review' and e['state']=='running')
        self.assertTrue(all(next(i for i,e in enumerate(events) if e['task_id']==t and e['state']=='succeeded') < review_start
                            for t in ('analyze', 'prepare_tests')))

    def test_invalid_spawn_is_atomic(self):
        states, _ = self.execute([{'id': 'planner', 'spawn': [{'id': 'valid'}, {'id': 'bad', 'depends_on': ['missing']}]}])
        self.assertEqual(states, {'planner': 'failed'})

    def test_oversized_request_does_not_block_others(self):
        states, _ = self.execute([{'id': 'huge', 'resources': {'memory_bytes': 2*1024**3}},
                                  {'id': 'child', 'depends_on': ['huge']}, {'id': 'small'}])
        self.assertEqual(states, {'huge': 'unschedulable', 'child': 'skipped', 'small': 'succeeded'})

    def test_budget_and_agent_serialization(self):
        tasks = [{'id': str(i), 'agent': 'same', 'action': 'sleep', 'seconds': 0.02} for i in range(3)]
        _, events = self.execute(tasks, max_workers=3)
        active = 0
        for e in events:
            active += 1 if e['state'] == 'running' else -1
            self.assertIn(active, (0, 1))

    def test_context_dedup_and_authorization(self):
        store = ContextStore(Path(self.temp.name))
        a = store.put({'text': 'shared'}, ['a'])
        b = store.put({'text': 'shared'}, ['b'])
        self.assertEqual(a, b)
        self.assertEqual(len(list(Path(self.temp.name).iterdir())), 1)
        self.assertEqual(store.get(a, 'b'), {'text': 'shared'})
        with self.assertRaises(PermissionError):
            store.get(a, 'intruder')
        with self.assertRaises(PermissionError):
            store.get('../private', 'a')

    def test_resource_reservations_are_released(self):
        tasks = [{'id':str(i), 'action':'sleep', 'seconds':0.03,
                  'resources':{'cpu_milli':1000}} for i in range(3)]
        states, events = self.execute(tasks, max_workers=3,
                    budget={'cpu_milli':1000, 'memory_bytes':1024**3, 'pids':128})
        active = 0
        for e in events:
            active += 1 if e['state'] == 'running' else -1
            self.assertIn(active, (0, 1))
        self.assertTrue(all(s=='succeeded' for s in states.values()))

    def test_real_tool_and_persistent_events(self):
        states, events = self.execute([{'id':'tool', 'action':'command',
            'argv':[sys.executable, '-c', "from pathlib import Path; Path('artifact.txt').write_text('ok')"]}])
        self.assertEqual(states['tool'], 'succeeded')
        root = Path(self.temp.name) / events[0]['workflow_id']
        self.assertEqual((root/'tool-1/artifact.txt').read_text(), 'ok')
        self.assertEqual(len((root/'events.jsonl').read_text().splitlines()), 2)
        self.assertTrue((root/'summary.json').exists())

    def test_strict_mode_refuses_unsupported_root(self):
        with self.assertRaises((RuntimeError, OSError)):
            self.execute([{'id': 'a'}], cgroup_root=self.temp.name)

    @unittest.skipUnless(sys.platform == 'linux' and os.environ.get('AGENTRT_TEST_CGROUP'),
                         '需要 NUC 上专用、已委派的测试 cgroup 子树')
    def test_linux_cgroup_oom(self):
        root = os.environ['AGENTRT_TEST_CGROUP']
        states, events = self.execute([{'id':'oom', 'action':'command',
            'resources':{'memory_bytes':128*1024**2},
            'argv':[sys.executable, '-c', 'x=bytearray(512*1024**2)']}], cgroup_root=root)
        self.assertEqual(states['oom'], 'failed')
        self.assertIn('OOM', events[-1]['error'])
        self.assertEqual(events[0]['enforcement'], 'cgroup_v2')


if __name__ == '__main__':
    unittest.main()
