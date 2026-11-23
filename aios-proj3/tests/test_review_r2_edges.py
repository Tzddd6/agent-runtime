from pathlib import Path
from tempfile import TemporaryDirectory
import threading,time,unittest
from unittest.mock import patch
from aios.context.context_store import ContextStore
from cerebrum.llm.apis import LLMQuery,LLMResponse
from runtime.launch import AIOSKernelRuntime


class RevisionTwoEdgeReview(unittest.TestCase):
    def test_ipc_reference_remains_readable_until_recipient_releases(self):
        with TemporaryDirectory() as folder,AIOSKernelRuntime(storage_root=folder) as kernel:
            sender=kernel.task_scope('sender');recipient=kernel.task_scope('recipient')
            sender.__enter__();recipient.__enter__();closed=False
            try:
                key=sender.put_context({'result':'needed by receiver'})
                kernel.ipc_manager.send('sender','recipient',{},context_refs=[key],token=sender.ipc_token)
                self.assertEqual(kernel.context_store.get(key,'recipient'),{'result':'needed by receiver'})
                sender.__exit__(None,None,None);closed=True
                messages=kernel.ipc_manager.receive('recipient',token=recipient.ipc_token)
                self.assertEqual(messages[0]['context_refs'],[key])
                self.assertEqual(kernel.context_store.get(key,'recipient'),{'result':'needed by receiver'})
            finally:
                if not closed:sender.__exit__(None,None,None)
                recipient.__exit__(None,None,None)

    def test_unlink_failure_is_not_reported_as_successful_reclamation(self):
        with TemporaryDirectory() as folder:
            store=ContextStore(folder);key=store.put('owner',{'private':'data'})
            path=Path(store._meta[key]['file_path'])
            with patch('aios.context.context_store.os.unlink',side_effect=PermissionError('synthetic file busy')):
                try:
                    result=store.delete(key,'owner')
                except OSError:
                    return
            self.assertFalse(result and path.exists(),'delete returned True while physical object still exists')

    def test_stop_signals_requests_waiting_outside_executor(self):
        gate=threading.Event();entered=threading.Event();lock=threading.Lock();active=[0]
        class Fixed:
            def complete(self,query):
                with lock:
                    active[0]+=1
                    if active[0]==8:entered.set()
                gate.wait(3)
                return LLMResponse(response_message='fixed')
        with TemporaryDirectory() as folder:
            kernel=AIOSKernelRuntime(storage_root=folder);kernel.llm.model_service=Fixed()
            def enqueue(i):
                sc=kernel.syscall_executor.create_syscall('a'+str(i),LLMQuery(model='fixed'))
                sc.set_created_time(time.time());kernel.queues['llm'].put(sc);return sc
            calls=[enqueue(i) for i in range(8)]
            kernel.start();stopper=None
            try:
                self.assertTrue(entered.wait(2))
                calls.extend(enqueue(i) for i in range(8,12))
                deadline=time.monotonic()+1
                while not kernel.queues['llm'].empty() and time.monotonic()<deadline:time.sleep(.002)
                self.assertTrue(kernel.queues['llm'].empty())
                calls.extend(enqueue(i) for i in range(12,16))
                stopper=threading.Thread(target=kernel.stop);stopper.start()
                deadline=time.monotonic()+1
                while kernel.scheduler.active and time.monotonic()<deadline:time.sleep(.002)
                gate.set();stopper.join(3)
                self.assertFalse(stopper.is_alive())
                self.assertTrue(all(sc.event.is_set() for sc in calls),'accepted request was stranded after stop')
            finally:
                gate.set()
                if stopper:stopper.join(4)
                else:kernel.stop()
