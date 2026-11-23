from pathlib import Path
from tempfile import TemporaryDirectory
import threading,time,unittest
from unittest.mock import Mock
from cerebrum.llm.apis import LLMQuery, LLMResponse
from runtime.launch import AIOSKernelRuntime


class ExpandedSchedulerTests(unittest.TestCase):
    def test_cloud_executor_backlog_is_bounded(self):
        gate=threading.Event();entered=threading.Event();lock=threading.Lock();count=[0]
        class FixedProvider:
            def complete(self,q):
                with lock:
                    count[0]+=1
                    if count[0]>=8:entered.set()
                gate.wait(3)
                return LLMResponse(response_message='fixed')
        with TemporaryDirectory() as folder:
            kernel=AIOSKernelRuntime(storage_root=folder)
            kernel.llm.model_service=FixedProvider()
            calls=[]
            for i in range(200):
                sc=kernel.syscall_executor.create_syscall('agent'+str(i),LLMQuery(model='fixed'))
                sc.set_created_time(time.time());calls.append(sc);kernel.queues['llm'].put(sc)
            kernel.start()
            try:
                self.assertTrue(entered.wait(2))
                time.sleep(.03)
                self.assertLessEqual(kernel.scheduler._cloud_pool._work_queue.qsize(),8)
            finally:
                gate.set();kernel.stop()
                self.assertTrue(all(sc.event.is_set() for sc in calls))
                self.assertEqual(kernel.scheduler.drf_controller.used.memory_bytes,0)

    def test_reader_typeerror_does_not_trigger_a_second_read(self):
        with TemporaryDirectory() as folder:
            kernel=AIOSKernelRuntime(storage_root=folder)
            reader=Mock(side_effect=TypeError('synthetic internal reader error'))
            kernel.scheduler.get_llm_syscall=reader
            try:
                with self.assertRaises(TypeError):kernel.scheduler._pop_llm_syscall(.01)
                self.assertEqual(reader.call_count,1)
            finally:kernel.stop()

    def test_local_batch_deadline_is_passed_to_reader(self):
        with TemporaryDirectory() as folder:
            kernel=AIOSKernelRuntime(storage_root=folder,batch_interval=.007)
            original=kernel.scheduler.get_llm_syscall;timeouts=[]
            def reader(timeout=.05):
                timeouts.append(timeout);return original(timeout)
            kernel.scheduler.get_llm_syscall=reader
            kernel.start()
            try:
                result=kernel.execute_request('local',LLMQuery(model='mock-aios-core',messages=[{'role':'user','content':'fixture'}]))
                self.assertEqual(result['response'].status_code,200)
                self.assertTrue(any(0<t<=.007 for t in timeouts))
            finally:kernel.stop()
