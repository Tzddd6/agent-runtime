from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from aios.context.context_store import ContextStore
from aios.scheduler.drf_admission import ResourceVector
from cerebrum.tool.apis import ToolQuery
from runtime.launch import AIOSKernelRuntime


class NativeBudgetAndCleanupTest(unittest.TestCase):
    def test_multibyte_context_views_respect_exact_byte_limit(self):
        with TemporaryDirectory() as folder:
            store = ContextStore(folder)
            identity = store.put('owner', {'goal': '复杂目标' * 200, 'constraints': ['完整约束' * 200]})
            for budget in (2, 32, 128, 400, 1024):
                view = store.build_budget_view(identity, 'owner', byte_budget=budget)
                self.assertLessEqual(view['view_bytes'], budget)
                self.assertTrue(view['truncated'])

    def test_unconfirmed_cleanup_keeps_reservation_and_blocks_agent(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            service, drf = kernel.process_service, kernel.scheduler.drf_controller
            cleanup = service.executor.cleanup
            query = ToolQuery(tool_calls=[{'name': 'math.calculate', 'parameters': {'expression': '6*7'}}])
            try:
                with patch.object(service.executor, 'cleanup', side_effect=RuntimeError('synthetic cleanup uncertainty')):
                    self.assertGreaterEqual(kernel.execute_request('uncertain', query)['response'].status_code, 400)
                self.assertIn('uncertain', service.quarantined_agents)
                self.assertEqual(drf.used.cpu_milli, 250)
                with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                    kernel.execute_request('uncertain', query)
            finally:
                # Explicit test reconciliation after verifying actual child reclamation.
                for identity, (agent, handle) in list(service.active.items()):
                    cleanup(handle)
                    service.active.pop(identity)
                service.quarantined_agents.clear()
                held = drf.get_or_create_agent('uncertain').allocated
                drf.release('uncertain', ResourceVector(held.cpu_milli, held.memory_bytes, held.pids))

    def test_recovery_rejects_changed_inputs(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            tasks = [{'id': 'node', 'query_type': 'tool', 'query_data': {
                'tool_calls': [{'name': 'math.calculate', 'parameters': {'expression': '6*7'}}]}}]
            kernel.workflow_engine.run_workflow('plan-test', tasks)
            tasks[0]['query_data']['tool_calls'][0]['parameters']['expression'] = '6*8'
            with self.assertRaisesRegex(RuntimeError, 'plan changed'):
                kernel.workflow_engine.run_workflow('plan-test', tasks, resume=True)
