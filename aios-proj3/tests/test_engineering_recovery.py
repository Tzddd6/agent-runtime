import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch
import zipfile

from aios.scheduler.workflow_checkpoint import WorkflowCheckpoint
from cerebrum.storage.apis import StorageQuery
from runtime.engineering_recovery import ReconciliationRequired
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole


DEMO = {'preset': 'engineering', 'project': 'json_pointer', 'deterministic_stub': True,
        'interrupt_verification': True}


def wait_for_job(console, identity):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        job = console.detail(identity)
        if job['status'] != 'running' and not console.conversations.lock.locked():
            return job
        sleep(.02)
    raise TimeoutError('Engineering job did not finish')


class EngineeringRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.cgroup = os.environ.get('AIOS_TEST_CGROUP_ROOT')

    def kernel(self):
        return AIOSKernelRuntime(storage_root=str(self.root / 'store'), cgroup_root=self.cgroup)

    def test_resume_after_kernel_restart_reuses_patch_and_verifies_zip(self):
        with self.kernel() as kernel:
            console = NativeConsole(kernel, self.root / 'jobs')
            try:
                identity = console.submit({**DEMO, 'include_audit_branch': True})['id']
                interrupted = wait_for_job(console, identity)
                self.assertEqual(interrupted['status'], 'interrupted')
                self.assertEqual(interrupted['model_budget']['calls'], 1)
                checkpoint = kernel.workflow_engine.load_checkpoint(identity)
                patch_ref = checkpoint['context_refs']['patch']
                candidate_sha = checkpoint['results']['patch']['model_receipt']['candidate_sha256']
                worker = next(row['worker'] for row in checkpoint['events'] if row.get('worker'))
                self.assertNotEqual(worker['returncode'], 0)
                self.assertTrue(worker['reclaimed'])
                if self.cgroup:
                    self.assertEqual(worker['enforcement'], 'cgroup_v2')
                    self.assertFalse(Path(worker['cgroup_path']).exists())
            finally:
                console.close()

        with self.kernel() as kernel:
            console = NativeConsole(kernel, self.root / 'jobs')
            try:
                with patch.object(kernel.scheduler.drf_controller, 'consume_model_budget',
                                  side_effect=AssertionError('Recovery tried to charge a model call')):
                    self.assertEqual(console.resume_job(identity)['id'], identity)
                    recovered = wait_for_job(console, identity)
                self.assertEqual(recovered['status'], 'succeeded')
                self.assertEqual(recovered['model_budget']['calls'], 1)
                self.assertEqual(recovered['attempts']['patch'], 1)
                self.assertEqual(recovered['attempts']['verify'], 2)
                self.assertEqual(recovered['context_refs']['patch'], patch_ref)
                self.assertEqual(recovered['engineering']['source_sha256'], candidate_sha)
                self.assertEqual(len([row for row in recovered['events']
                                      if row.get('task_id') == 'patch' and row['state'] == 'running']), 1)
                with zipfile.ZipFile(io.BytesIO(console.download_zip(identity))) as bundle:
                    evidence = json.loads(bundle.read('execution_evidence.json'))
                self.assertEqual(evidence['model_calls_during_resume'], 0)
                self.assertEqual(evidence['verified_source_sha256'], candidate_sha)
                first, second = evidence['verification_workers']
                self.assertNotEqual(first['id'], second['id'])
                self.assertEqual(first['parameters_sha256'], second['parameters_sha256'])
                self.assertEqual(second['returncode'], 0)
                self.assertTrue(second['reclaimed'])
                if self.cgroup:
                    self.assertFalse(Path(second['cgroup_path']).exists())
                with self.assertRaises(ReconciliationRequired):
                    console.resume_job(identity)
            finally:
                console.close()
        if self.cgroup:
            self.assertFalse(any(path.is_dir() for path in Path(self.cgroup).iterdir()))

    def test_tampered_checkpoint_does_not_dispatch_another_worker(self):
        with self.kernel() as kernel:
            console = NativeConsole(kernel, self.root / 'jobs')
            try:
                identity = console.submit(DEMO)['id']
                self.assertEqual(wait_for_job(console, identity)['status'], 'interrupted')
                store = WorkflowCheckpoint(kernel.workflow_engine.checkpoint_dir, identity)
                saved = store.load()
                saved['results']['patch']['model_receipt']['candidate_sha256'] = '0' * 64
                with store.writer() as writer:
                    writer.plan_signature = saved['plan_signature']
                    writer.snapshot(saved['states'], saved['results'], saved['context_refs'], saved['events'])
                console.resume_job(identity)
                job = wait_for_job(console, identity)
                self.assertEqual(job['status'], 'needs_reconciliation')
                self.assertFalse(job['recovery_available'])
                self.assertEqual(sum(row['operation'] == 'project.patch_check'
                                     for row in kernel.process_service.snapshot()['receipts']), 1)
            finally:
                console.close()

    def test_lost_delivery_acknowledgement_requires_reconciliation(self):
        with self.kernel() as kernel:
            console = NativeConsole(kernel, self.root / 'jobs')
            original = kernel.execute_request
            writes = []

            def lost_reply(agent, query):
                result = original(agent, query)
                if isinstance(query, StorageQuery) and query.operation_type == 'write':
                    writes.append(query.params['path'])
                    raise ConnectionError('Unconfirmed delivery')
                return result

            try:
                with patch.object(kernel, 'execute_request', side_effect=lost_reply):
                    identity = console.submit({**DEMO, 'interrupt_verification': False})['id']
                    job = wait_for_job(console, identity)
                self.assertEqual(job['status'], 'needs_reconciliation')
                self.assertEqual(kernel.workflow_engine.load_checkpoint(identity)['states']['answer'],
                                 'needs_reconciliation')
                self.assertEqual(len(writes), 1)
                with self.assertRaises(ReconciliationRequired):
                    console.resume_job(identity)
                with self.assertRaises(ValueError):
                    console.download_zip(identity)
            finally:
                console.close()
