"""Offline engineering web integration; no network or model calls."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from agent_runtime.model_gateway import ModelError
from agent_runtime.web import Controller


class RepairWebTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parent/'tmp'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        with patch.dict(os.environ, {}, clear=True):
            self.controller = Controller(self.temp.name)
        self.controller.machine.sample = Mock(return_value={'service': {
            'cpu_limit_cores': 4, 'memory_limit_bytes': 2 * 1024**3}})

    def submit(self, workers=2, accepted=False, validation=True):
        def fake_run(tasks, workers, timeout, **kw):
            counts = {'public': {'total': 3, 'passed': 3, 'failed': 0},
                      'heldout': {'total': 2, 'passed': 2, 'failed': 0}}
            outputs = {'baseline': {**counts, 'public': {'total':3, 'passed':1, 'failed':2},
                                     'expected_red': True, 'baseline_failing_count': 2},
                       'analyze': 'diagnosis', 'test_plan': 'boundary checks', 'repair': '{}',
                       'validate': {**counts, 'success': validation, 'source': 'def sample(): return 1',
                                    'source_sha256': 'abc123'}}
            if validation:
                outputs.update(review='review response', gate={'accepted': accepted,
                    'reason': 'offline verdict', 'source_sha256': 'abc123'})
            events = []
            for index, (task_id, output) in enumerate(outputs.items()):
                result = {'result': output}
                if task_id in ('analyze', 'test_plan', 'repair', 'review'):
                    result['model_call'] = {'usage': {'prompt_tokens': 10, 'completion_tokens': 20}}
                events.append({'task_id': task_id, 'state': 'succeeded', 'attempt': 1,
                               'time': index, 'result': result})
            folder = Path(kw['output_dir'])/'run'
            folder.mkdir(parents=True)
            (folder/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events), encoding='utf8')
            return {key: 'succeeded' for key in outputs}, events
        with patch.object(self.controller, 'model_info', return_value={
                'configured': True, 'available': True, 'model': 'offline-test'}), \
             patch('agent_runtime.web.run', side_effect=fake_run):
            key = self.controller.submit({'preset': 'engineering', 'project': 'pricing', 'workers': workers})
            deadline = time.monotonic()+3
            while time.monotonic() < deadline:
                with self.controller.lock:
                    finished = 'finished' in self.controller.jobs[key]
                if finished:
                    return self.controller.detail(key)
                time.sleep(.005)
            self.fail('mock run did not finish')

    def test_os_success_cannot_override_rejected_business_verdict(self):
        job = self.submit()
        self.assertTrue(all(s == 'succeeded' for s in job['states'].values()))
        self.assertEqual(job['status'], 'failed')
        view = job['engineering']
        self.assertFalse(view['verdict']['accepted'])
        self.assertEqual(next(p for p in view['phases'] if p['id']=='gate')['status'], 'failed')
        self.assertEqual(view['model_calls'], 4)
        self.assertEqual(view['usage']['completion_tokens'], 80)
        self.assertEqual(view['patch_source'], 'def sample(): return 1')
        self.assertEqual(view['source_sha256'], 'abc123')
        self.assertEqual(view['baseline']['baseline_failing_count'], 2)

    def test_failed_validation_skips_review(self):
        job = self.submit(validation=False)
        self.assertEqual(job['status'], 'failed')
        self.assertNotIn('review', job['states'])
        phases = {p['id']:p['status'] for p in job['engineering']['phases']}
        self.assertEqual(phases['validate'], 'failed')
        self.assertEqual(phases['review'], 'skipped')
        self.assertEqual(job['engineering']['model_calls'], 3)

    def test_same_total_budget_workers_one_and_two(self):
        serial, parallel = self.submit(workers=1, accepted=True), self.submit(workers=2, accepted=True)
        self.assertEqual(serial['status'], 'succeeded')
        self.assertEqual(parallel['status'], 'succeeded')
        expected = {'cpu_milli': 2000, 'memory_bytes': 1024**3, 'pids': 128}
        self.assertEqual(serial['budget'], expected)
        self.assertEqual(parallel['budget'], expected)
        self.assertEqual(parallel['engineering']['budget'], expected)

    def test_both_workers_respect_actual_service_cap(self):
        self.controller.machine.sample.return_value = {'service': {
            'cpu_limit_cores': .75, 'memory_limit_bytes': 768*1024**2}}
        serial, parallel = self.submit(workers=1, accepted=True), self.submit(workers=2, accepted=True)
        self.assertEqual(serial['budget'], parallel['budget'])
        self.assertEqual(serial['budget']['cpu_milli'], 750)
        self.assertEqual(serial['budget']['memory_bytes'], 640*1024**2)

    def test_missing_key_or_unavailable_broker_stops_before_run(self):
        for available in (True, False):
            with self.subTest(available=available), patch.object(self.controller, 'model_info', return_value={
                    'configured': False, 'available': available, 'model': 'offline-test'}), \
                 patch('agent_runtime.web.run') as run:
                with self.assertRaises(ValueError):
                    self.controller.submit({'preset':'engineering', 'project':'pricing'})
                run.assert_not_called()
        self.assertFalse(self.controller.jobs)

    def test_broker_status_has_no_key_and_reports_unavailability(self):
        broker = Mock(model='offline-test')
        broker.status.return_value = {'configured':True, 'model':'offline-test'}
        self.controller.model_broker = broker
        snapshot = self.controller.snapshot()
        self.assertTrue(snapshot['model_configured'])
        self.assertTrue(snapshot['model_available'])
        self.assertNotIn('key', snapshot)
        broker.status.side_effect = ModelError('offline broker unavailable')
        snapshot = self.controller.snapshot()
        self.assertFalse(snapshot['model_configured'])
        self.assertFalse(snapshot['model_available'])
        self.assertEqual(snapshot['model'], 'offline-test')


if __name__ == '__main__':
    unittest.main()
