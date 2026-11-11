import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from agent_runtime.web import Controller
from agent_runtime.job_store import JobStore,MAX_BYTES
from agent_runtime.telemetry import effective_limits


class ConsolePersistenceTests(unittest.TestCase):
    def test_finished_job_restores_without_reexecution_or_secret_persistence(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'DEEPSEEK_API_KEY':'offline-never-call'}):
            controller=Controller(directory)
            key=controller.submit({'preset':'dynamic'})
            deadline=time.monotonic()+8
            while controller.jobs[key].get('finished') is None and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(controller.jobs[key]['status'],'succeeded')
            raw=(Path(directory)/key/'job.json').read_text()
            for secret in ('offline-never-call',controller.csrf,'_gateway_token','_ipc_token'):
                self.assertNotIn(secret,raw)
            with patch('agent_runtime.web.run',side_effect=AssertionError('must not execute on restore')):
                restored=Controller(directory)
                detail=restored.detail(key)
            self.assertTrue(detail['restored'])
            self.assertEqual(detail['status'],'succeeded')
            self.assertEqual(detail['states']['review'],'succeeded')
            self.assertTrue(detail['monitor']['historical'])

    def test_running_intent_is_interrupted_and_never_automatically_resubmitted(self):
        with tempfile.TemporaryDirectory() as directory:
            key='a'*32
            JobStore(directory).save({'id':key,'status':'running','created':1,'preset':'dynamic',
                'budget':{},'workers':2,'policy':'fifo','history':[],'monitor':None})
            with patch('agent_runtime.web.run',side_effect=AssertionError('no resume')):
                restored=Controller(directory)
            self.assertEqual(restored.jobs[key]['status'],'interrupted')
            self.assertIn('不会自动续跑',restored.detail(key)['error'])

    def test_corrupt_oversized_and_unrecognized_index_entries_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for index,data in enumerate([b'{broken',b'x'*(MAX_BYTES+1),b'{"schema":1,"status":"running"}']):
                folder=root/(str(index)*32);folder.mkdir();(folder/'job.json').write_bytes(data)
            self.assertEqual(JobStore(root).load(),[])
            with self.assertRaises(ValueError):JobStore(root).save({'id':'../escape'})

    def test_disk_error_refuses_submission_before_worker_or_paid_call(self):
        with tempfile.TemporaryDirectory() as directory:
            controller=Controller(directory)
            controller.store.save=Mock(side_effect=OSError('disk full fixture'))
            with patch('agent_runtime.web.run') as run:
                with self.assertRaises(OSError):controller.submit({'preset':'dynamic'})
                run.assert_not_called()
            self.assertEqual(controller.jobs,{})

    def test_ancestor_cpu_memory_cpuset_and_process_ceilings(self):
        with tempfile.TemporaryDirectory() as directory:
            mount=Path(directory);parent=mount/'parent';child=parent/'service';child.mkdir(parents=True)
            for node,quota,memory,pids in [(mount,'max 100000','max','max'),
                (parent,'75000 100000',str(512*1024**2),'96'),
                (child,'200000 100000',str(2*1024**3),'256')]:
                (node/'cpu.max').write_text(quota);(node/'memory.max').write_text(memory);(node/'pids.max').write_text(pids)
            (child/'cpuset.cpus.effective').write_text('0-3,6')
            limits=effective_limits(child,mount)
            self.assertEqual(limits['cpu_limit_cores'],.75)
            self.assertEqual(limits['memory_limit_bytes'],512*1024**2)
            self.assertEqual(limits['pids_limit'],96)
            with tempfile.TemporaryDirectory() as work:
                budget=Controller(work).resource_budget(limits)
            self.assertEqual(budget,{'cpu_milli':750,'memory_bytes':384*1024**2,'pids':80})
