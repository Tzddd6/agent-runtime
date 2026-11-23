import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aios.context.context_store import ContextStore
from runtime.launch import AIOSKernelRuntime


class NativeRecoveryTest(unittest.TestCase):
    def test_context_is_immutable_and_acl_survives_restart(self):
        with TemporaryDirectory() as folder:
            store = ContextStore(folder)
            value = {'claims': ['original']}
            identity = store.put('owner', value, authorized_agents=['reader'])
            value['claims'].append('mutated')
            visible = store.get(identity, 'reader')
            visible['claims'].append('also-mutated')
            restored = ContextStore(folder)
            self.assertEqual(restored.get(identity, 'reader'), {'claims': ['original']})
            with self.assertRaises(PermissionError):
                restored.get(identity, 'stranger')

    def test_completed_workflow_resumes_with_persisted_context(self):
        with TemporaryDirectory() as folder:
            root = str(Path(folder) / 'store')
            counter = []
            def prepare(*_):
                counter.append('ran')
                return {'value': 21}
            tasks = [{'id': 'prepare', 'handler': prepare},
                     {'id': 'finish', 'depends_on': ['prepare'], 'handler': lambda task, inputs, attempt: {'value': inputs['prepare']['value'] * 2}}]
            with AIOSKernelRuntime(storage_root=root) as first:
                self.assertEqual(first.workflow_engine.run_workflow('restart', tasks)['results']['finish']['value'], 42)
            with AIOSKernelRuntime(storage_root=root) as second:
                result = second.workflow_engine.run_workflow('restart', tasks, resume=True)
                self.assertEqual(result['results']['finish']['value'], 42)
            self.assertEqual(counter, ['ran'])

    def test_uncertain_inflight_operation_is_not_replayed(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            engine = kernel.workflow_engine
            called = []
            engine._save_checkpoint('uncertain', {'effect': 'running'}, {}, {}, [])
            with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                engine.run_workflow('uncertain', [{'id': 'effect', 'handler': lambda *_: called.append('effect')}], resume=True)
            self.assertFalse(called)
