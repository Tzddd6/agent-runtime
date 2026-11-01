import contextlib
import io
import unittest
from agent_runtime.__main__ import run, validate


class RuntimeTests(unittest.TestCase):
    def test_failure_only_skips_dependent_branch(self):
        tasks = [{'id': 'bad', 'action': 'fail'}, {'id': 'dependent', 'depends_on': ['bad']}, {'id': 'independent'}]
        with contextlib.redirect_stdout(io.StringIO()):
            states, _ = run(tasks)
        self.assertEqual(states, {'bad': 'failed', 'dependent': 'skipped', 'independent': 'succeeded'})

    def test_timeout(self):
        with contextlib.redirect_stdout(io.StringIO()):
            states, _ = run([{'id': 'slow', 'action': 'sleep', 'seconds': 3}], timeout=0.1)
        self.assertEqual(states['slow'], 'timeout')

    def test_rejects_cycle(self):
        with self.assertRaises(ValueError):
            validate([{'id': 'a', 'depends_on': ['b']}, {'id': 'b', 'depends_on': ['a']}])


if __name__ == '__main__':
    unittest.main()
