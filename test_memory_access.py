import tempfile
import unittest
from pathlib import Path
from agent_runtime.memory_access import MemoryContext, principal


class MemoryAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory = MemoryContext(Path(self.temp.name))

    def row(self, content='note', user='alice', owner='owner', policy='private', score=1):
        return {'content': content, 'score': score,
                'metadata': {'user_id': user, 'owner_agent': owner, 'sharing_policy': policy}}

    def select(self, rows, **kwargs):
        return self.memory.select(rows, user='alice', agent='owner', **kwargs)

    def test_exact_utf8_budget_including_line(self):
        row = self.row('你好')
        cost = len('- [] 你好\n'.encode())
        self.assertEqual(self.select([row], max_bytes=cost-1)[0], [])
        chosen, diag = self.select([row], max_bytes=cost)
        self.assertEqual(len(chosen), 1)
        self.assertEqual(diag['memory_bytes'], cost)

    def test_zero_limits(self):
        self.assertEqual(self.select([self.row()], max_bytes=0)[0], [])
        self.assertEqual(self.select([self.row()], max_memories=0)[0], [])

    def test_untrusted_metadata_types_fail_closed(self):
        rows = []
        for meta in (None, [], 'alice', 1, {'user_id': ['alice']},
                     {'user_id': 'alice', 'owner_agent': []},
                     {'user_id': 'alice', 'sharing_policy': 'shared'}):
            rows.append({'content': 'bad', 'score': 1, 'metadata': meta})
        self.assertEqual(self.select(rows)[0], [])

    def test_wrong_user_precedes_dedup_and_budget(self):
        rows = [self.row('same', user='bob'), self.row('same')]
        result, _ = self.select(rows, max_memories=1)
        self.assertEqual([r['content'] for r in result], ['same'])

    def test_storage_acl_checked_and_user_namespaced(self):
        rows, _ = self.select([self.row()])
        ref = rows[0]['ref']
        self.assertEqual(self.memory.store.get(ref, principal('alice', 'owner'))['content'], 'note')
        for user, agent in [('bob', 'owner'), ('alice', 'peer'), ('alice__kernel_shared', 'owner')]:
            with self.assertRaises(PermissionError):
                self.memory.store.get(ref, principal(user, agent))

    def test_shared_grant_does_not_cross_user(self):
        rows, _ = self.select([self.row(owner='peer', policy='shared')])
        ref = rows[0]['ref']
        self.memory.store.get(ref, principal('alice', 'owner'))
        with self.assertRaises(PermissionError):
            self.memory.store.get(ref, principal('bob', 'owner'))

    def test_dedup_and_nonfinite_relevance(self):
        rows, _ = self.select([self.row(), self.row(), self.row('nan', score=float('nan'))])
        self.assertEqual([r['content'] for r in rows], ['note'])

    def test_identity_pair_cannot_collide(self):
        self.assertNotEqual(principal('a:b', 'c'), principal('a', 'b:c'))


if __name__ == '__main__':
    unittest.main()
