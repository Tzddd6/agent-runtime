import json
from pathlib import Path
import tempfile
import unittest
from agent_runtime.context import ContextStore


class ContextCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_hits_do_not_bypass_authorization_or_share_mutable_values(self):
        store=ContextStore(self.root)
        ref=store.put({'items':[1,2]},['alice'])
        store.get(ref,'alice')['items'].append(99)
        with self.assertRaises(PermissionError):store.get(ref,'bob')
        self.assertEqual(store.get(ref,'alice'),{'items':[1,2]})
        self.assertEqual(store.stats()['disk_reads'],1)
        self.assertEqual(store.stats()['cache_hits'],1)

    def test_capacity_and_large_object_bypass(self):
        store=ContextStore(self.root,cache_bytes=40,cache_max_entries=2)
        for i in range(8):
            ref=store.put({'i':i},['a']);store.get(ref,'a')
            self.assertLessEqual(store.stats()['cache_bytes'],40)
            self.assertLessEqual(store.stats()['cache_entries'],2)
        large=store.put({'text':'x'*100},['a'])
        self.assertEqual(store.get(large,'a'),{'text':'x'*100})
        self.assertEqual(store.get(large,'a'),{'text':'x'*100})
        self.assertEqual(store.stats()['cache_bypasses'],2)
        self.assertGreater(store.stats()['cache_evictions'],0)

    def test_lru2_preserves_repeated_context_during_one_pass_scan(self):
        def scan(policy,path):
            store=ContextStore(path,cache_bytes=4096,cache_max_entries=2,cache_policy=policy)
            hot=store.put({'id':'hot'},['a'])
            store.get(hot,'a');store.get(hot,'a')
            for key in ('cold-1','cold-2','cold-3'):
                ref=store.put({'id':key},['a']);store.get(ref,'a')
            reads=store.stats()['disk_reads']
            self.assertEqual(store.get(hot,'a'),{'id':'hot'})
            return store.stats()['disk_reads']-reads
        self.assertEqual(scan('lru2',self.root/'lru2'),0)
        self.assertEqual(scan('lru',self.root/'lru'),1)

    def test_tampered_disk_invalidates_a_hot_cache(self):
        store=ContextStore(self.root)
        ref=store.put({'text':'original'},['a'])
        store.get(ref,'a');store.get(ref,'a')
        (self.root/ref).write_text(json.dumps({'text':'changed'}),encoding='utf-8')
        with self.assertRaises(ValueError):store.get(ref,'a')
        self.assertEqual(store.stats()['cache_invalidations'],1)
        self.assertEqual(store.stats()['cache_entries'],0)

    def test_zero_budget_is_a_read_through_baseline(self):
        store=ContextStore(self.root,cache_bytes=0)
        ref=store.put({'text':'same'},['a'])
        for _ in range(3):self.assertEqual(store.get(ref,'a'),{'text':'same'})
        self.assertEqual(store.stats()['disk_reads'],3)
        self.assertEqual(store.stats()['cache_bytes'],0)
        self.assertEqual(store.stats()['cache_hits'],0)

    def test_oversized_corruption_is_rejected_before_reading(self):
        store=ContextStore(self.root,cache_bytes=128)
        ref=store.put({'text':'original'},['a'])
        store.get(ref,'a')
        with (self.root/ref).open('wb') as target:
            target.truncate(2*1024*1024)
        with self.assertRaises(ValueError):store.get(ref,'a')
        self.assertEqual(store.stats()['disk_reads'],1)
        self.assertEqual(store.stats()['cache_bytes'],0)


if __name__=='__main__':unittest.main()
