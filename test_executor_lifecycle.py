import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from agent_runtime.executor import Executor


LIMITS={'cpu_milli':1000,'memory_bytes':128*1024**2,'pids':32}


class ExecutorLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_large_payload_uses_one_byte_gate(self):
        original=subprocess.Popen
        writes=[]
        class Pipe:
            def __init__(self,stream):self.stream=stream
            @property
            def closed(self):return self.stream.closed
            def write(self,value):writes.append(value);return self.stream.write(value)
            def close(self):return self.stream.close()
        def create(*args,**kwargs):
            proc=original(*args,**kwargs)
            proc.stdin=Pipe(proc.stdin)
            return proc
        executor=Executor()
        with patch('agent_runtime.executor.subprocess.Popen',side_effect=create):
            handle=executor.launch({'id':'big','message':'ok','contexts':[{'text':'x'*500000}],
                                    'timeout':5},self.root/'big',LIMITS)
        self.addCleanup(executor.cleanup,handle)
        self.assertEqual(writes,['G'])
        self.assertEqual(handle['proc'].wait(timeout=5),0)
        self.assertEqual(executor.cleanup(handle)[0],0)
        self.assertFalse((self.root/'big/.payload.json').exists())

    def test_worker_does_not_execute_before_gate_and_legacy_stdin_still_works(self):
        payload=self.root/'payload.json'; result=self.root/'result.json'
        payload.write_text(json.dumps({'id':'gated','message':'ok'}),encoding='utf-8')
        proc=subprocess.Popen([sys.executable,'-m','agent_runtime.worker',str(result),str(payload)],
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True)
        try:
            time.sleep(.1)
            self.assertFalse(result.exists())
            proc.communicate('G',timeout=5)
            self.assertEqual(proc.returncode,0)
            self.assertEqual(json.loads(result.read_text())['task_id'],'gated')
            self.assertFalse(payload.exists())
        finally:
            if proc.poll() is None:proc.kill();proc.wait()
        legacy=subprocess.run([sys.executable,'-m','agent_runtime.worker',str(result)],
            input=json.dumps({'id':'legacy'}),text=True,stdout=subprocess.DEVNULL,timeout=5)
        self.assertEqual(legacy.returncode,0)
        self.assertEqual(json.loads(result.read_text())['task_id'],'legacy')

    def test_deadline_watchdog_and_cleanup_are_idempotent(self):
        executor=Executor()
        handle=executor.launch({'id':'slow','action':'sleep','seconds':5,'timeout':.2},self.root/'slow',LIMITS)
        self.addCleanup(executor.cleanup,handle)
        handle['proc'].wait(timeout=3)
        self.assertTrue(handle['expired'].is_set())
        first=executor.cleanup(handle)
        self.assertNotEqual(first[0],0)
        self.assertIs(executor.cleanup(handle),first)
        self.assertFalse((self.root/'slow/.payload.json').exists())

    def test_missing_log_does_not_turn_reclaimed_process_into_cleanup_failure(self):
        executor=Executor()
        handle=executor.launch({'id':'ok','timeout':5},self.root/'ok',LIMITS)
        self.addCleanup(executor.cleanup,handle)
        handle['proc'].wait(timeout=5)
        handle['output'].close()
        (self.root/'ok/worker.log').unlink()
        result=executor.cleanup(handle)
        self.assertEqual(result[0],0)
        self.assertIn('日志不可读',result[1])
        self.assertIs(executor.cleanup(handle),result)

    def test_startup_time_is_counted_before_popen_returns(self):
        original=subprocess.Popen
        def delayed(*args,**kwargs):
            time.sleep(.15)
            return original(*args,**kwargs)
        executor=Executor()
        before=time.monotonic()
        with patch('agent_runtime.executor.subprocess.Popen',side_effect=delayed):
            handle=executor.launch({'id':'late','action':'sleep','seconds':5,'timeout':.05},self.root/'late',LIMITS)
        self.addCleanup(executor.cleanup,handle)
        handle['proc'].wait(timeout=3)
        self.assertLess(handle['started']-before,.05)
        self.assertTrue(handle['expired'].is_set())
        self.assertNotEqual(executor.cleanup(handle)[0],0)

    @unittest.skipUnless(sys.platform=='linux' and os.environ.get('AGENTRT_TEST_CGROUP'),'需要真实委派 cgroup')
    def test_linux_tiny_cpu_budget_large_payload_still_has_a_deadline(self):
        executor=Executor(os.environ['AGENTRT_TEST_CGROUP'])
        before=time.monotonic()
        handle=executor.launch({'id':'tiny','message':'ok','contexts':[{'text':'x'*500000}],
                               'timeout':.2},self.root/'tiny',{**LIMITS,'cpu_milli':10})
        self.addCleanup(executor.cleanup,handle)
        handle['proc'].wait(timeout=3)
        self.assertLess(time.monotonic()-before,3)
        self.assertTrue(handle['expired'].is_set())
        self.assertNotEqual(executor.cleanup(handle)[0],0)


if __name__=='__main__':unittest.main()
