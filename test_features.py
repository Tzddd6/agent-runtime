import contextlib
import io
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent_runtime.context import ContextStore
from agent_runtime.ipc import Broker
from agent_runtime.runtime import run


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_compression_integrity_and_reuse(self):
        store = ContextStore(self.root)
        value = {'text': 'shared context\n' * 2000}
        ref = store.put(value, ['alice'])
        self.assertEqual(ref, store.put(value, ['bob']))
        self.assertEqual(store.get(ref, 'bob'), value)
        stats = store.stats()
        self.assertLess(stats['stored_bytes'], stats['logical_bytes'] // 10)
        self.assertEqual(stats['unique_objects'], 1)
        (self.root / ref).write_bytes(b'{}')
        with self.assertRaises(ValueError):
            store.get(ref, 'alice')

    def test_mailbox_dedup_ack_and_capability_revocation(self):
        broker = Broker(ContextStore(self.root), ['a', 'b'], capacity=1)
        self.addCleanup(broker.close)
        a, b = broker.issue('a'), broker.issue('b')
        request = dict(op='send', to='b', request_id='r1', data={'text':'hello'})
        receipt = broker.dispatch(a, request)
        self.assertEqual(receipt, broker.dispatch(a, request))
        with self.assertRaises(ValueError):
            broker.dispatch(a, {**request, 'data':'different'})
        with self.assertRaises(ValueError):
            broker.dispatch(a, {**request, 'request_id':'r2'})
        self.assertIsNone(broker.dispatch(a, {'op':'receive'})['message'])
        self.assertEqual(broker.dispatch(b, {'op':'get', 'ref':receipt['ref']})['data'], request['data'])
        self.assertEqual(broker.dispatch(b, {'op':'receive'})['message'], receipt)
        broker.dispatch(b, dict(op='ack', ref=receipt['ref'], request_id='r1'))
        self.assertIsNone(broker.dispatch(b, {'op':'receive'})['message'])
        broker.revoke(a)
        with self.assertRaises(PermissionError):
            broker.dispatch(a, request)
        with self.assertRaises(PermissionError):
            broker.dispatch(b, {**request, 'to':'outsider'})

    def test_real_cross_worker_communication(self):
        tasks = [{'id':'reader', 'agent':'consumer', 'action':'receive', 'seconds':3},
                 {'id':'sender', 'agent':'producer', 'action':'send',
                  'to':'consumer', 'data':{'text':'真实进程通信'}, 'request_id':'greeting'}]
        with contextlib.redirect_stdout(io.StringIO()):
            states, events = run(tasks, output_dir=self.root)
        self.assertTrue(all(s == 'succeeded' for s in states.values()), events)
        received = next(e['result']['result'] for e in events
                        if e['task_id']=='reader' and e['state']=='succeeded')
        self.assertEqual(received, {'text':'真实进程通信'})
        self.assertNotEqual(events[0]['pid'], events[1]['pid'])

    def test_real_document_pipeline_and_input_validation(self):
        from tools.run_document import tasks_for
        with contextlib.redirect_stdout(io.StringIO()):
            states, events = run(tasks_for('hello world hello'), output_dir=self.root)
        self.assertTrue(all(s == 'succeeded' for s in states.values()), events)
        root = self.root / events[0]['workflow_id']
        report = json.loads((root / 'review-1/report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['report']['analyze']['words'], 3)
        self.assertEqual(report['report']['index']['unique_words'], 2)
        with self.assertRaises(ValueError):
            run([{'id':'a'}, {'id':'b', 'inputs':['a']}], output_dir=self.root)

    def test_model_team_passes_real_upstream_results(self):
        from agent_runtime.model_gateway import ModelGateway
        from agent_runtime.scenarios import team_tasks
        class Client:
            model, timeout = 'offline-fixture', 5
            def __init__(self):
                self.messages = []
            def complete(self, messages, max_tokens):
                self.messages.append(messages)
                return {'content':'fixture-answer', 'model':self.model, 'usage':{}}
        client = Client()
        gateway = ModelGateway(client, self.root/'calls.jsonl')
        with patch('agent_runtime.runtime.ModelGateway.from_env', return_value=gateway):
            with contextlib.redirect_stdout(io.StringIO()):
                states, events = run(team_tasks('explain scheduling'), output_dir=self.root)
        self.assertTrue(all(s=='succeeded' for s in states.values()), events)
        self.assertEqual(len(client.messages), 4)
        last = json.loads(client.messages[-1][-1]['content'])
        self.assertEqual(last['upstream_results'], {'analyze':'fixture-answer','test_plan':'fixture-answer'})

    @unittest.skipUnless(sys.platform == 'linux' and os.environ.get('AGENTRT_TEST_CGROUP'), '需要 Linux 委派 cgroup')
    def test_linux_pids_limit(self):
        code = """import subprocess, errno
children=[]
try:
    for i in range(100): children.append(subprocess.Popen(['sleep','5']))
except OSError as exc:
    assert exc.errno == errno.EAGAIN, exc
    print('PIDS_LIMIT_OBSERVED', flush=True)
else:
    raise AssertionError('pids limit did not trigger')
"""
        with contextlib.redirect_stdout(io.StringIO()):
            states, events = run([{'id':'pids','action':'command', 'resources':{'pids':8},
                'argv':[sys.executable,'-c',code]}], output_dir=self.root,
                cgroup_root=os.environ['AGENTRT_TEST_CGROUP'])
        self.assertEqual(states['pids'], 'succeeded', events)
        self.assertIn('PIDS_LIMIT_OBSERVED', events[-1]['output'])
        self.assertRegex(events[-1]['usage']['pids.events'], r'max [1-9]')

    @unittest.skipUnless(sys.platform == 'linux' and os.environ.get('AGENTRT_TEST_CGROUP'), '需要 Linux 委派 cgroup')
    def test_linux_detached_descendant_cleanup(self):
        code = "import subprocess; subprocess.Popen(['sleep','30'], start_new_session=True)"
        root = Path(os.environ['AGENTRT_TEST_CGROUP'])
        before = {p.name for p in root.iterdir() if p.is_dir()}
        with contextlib.redirect_stdout(io.StringIO()):
            states, events = run([{'id':'detached','action':'command',
                'argv':[sys.executable,'-c',code]}], output_dir=self.root, cgroup_root=root)
        self.assertEqual(states['detached'], 'succeeded', events)
        self.assertEqual({p.name for p in root.iterdir() if p.is_dir()}, before)

    @unittest.skipUnless(sys.platform == 'linux' and os.environ.get('AGENTRT_TEST_CGROUP'), '需要 Linux 委派 cgroup')
    def test_linux_timeout_cleanup_and_retry(self):
        root = Path(os.environ['AGENTRT_TEST_CGROUP'])
        before = {p.name for p in root.iterdir() if p.is_dir()}
        with contextlib.redirect_stdout(io.StringIO()):
            states, events = run([{'id':'timeout','action':'sleep','seconds':5,
                'timeout':.2, 'retries':1}, {'id':'independent'}], output_dir=self.root, cgroup_root=root)
        self.assertEqual(states, {'timeout':'timeout', 'independent':'succeeded'})
        self.assertEqual(len([e for e in events if e['task_id']=='timeout' and e['state']=='running']), 2)
        self.assertEqual({p.name for p in root.iterdir() if p.is_dir()}, before)


if __name__ == '__main__':
    unittest.main()
