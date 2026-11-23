from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from aios.context.context_store import ContextStore
from runtime.launch import AIOSKernelRuntime


class IndependentTaskScopeReview(unittest.TestCase):
    def test_deduplicated_context_survives_other_scope_exit(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=folder) as kernel:
            first=kernel.task_scope('scope-first');second=kernel.task_scope('scope-second')
            first.__enter__();second.__enter__()
            closed=False
            try:
                a=first.put_context({'shared':'same-content'})
                b=second.put_context({'shared':'same-content'})
                self.assertEqual(a,b)
                first.__exit__(None,None,None);closed=True
                self.assertEqual(kernel.context_store.get(b,'scope-second'),{'shared':'same-content'})
            finally:
                if not closed:first.__exit__(None,None,None)
                second.__exit__(None,None,None)

    def test_duplicate_active_scope_name_is_rejected(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=folder) as kernel:
            with kernel.task_scope('same-name'):
                other=kernel.task_scope('same-name');entered=False
                try:
                    with self.assertRaises((RuntimeError,ValueError)):
                        other.__enter__();entered=True
                finally:
                    if entered:other.__exit__(None,None,None)

    def test_failed_delete_preserves_recoverable_object(self):
        with TemporaryDirectory() as folder:
            store=ContextStore(folder);key=store.put('owner',{'value':1})
            path=Path(store._meta[key]['file_path'])
            with patch('aios.context.context_store.sqlite3.connect',side_effect=OSError('synthetic SQLite outage')):
                with self.assertRaises(OSError):store.delete(key,'owner')
            self.assertIn(key,store._meta)
            self.assertTrue(path.exists())
            self.assertEqual(ContextStore(folder).get(key,'owner'),{'value':1})
