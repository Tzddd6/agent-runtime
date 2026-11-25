import json,time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4
from cerebrum.llm.apis import LLMResponse
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole
from aios.tool import engineering

class IndependentDeliveryReview(unittest.TestCase):
    def fixture(self,kernel,folder):
        return NativeConsole(kernel,Path(folder)/'jobs')
    def run_good(self,console):
        value={'conversation_id':uuid4().hex,'request_id':uuid4().hex,'mode':'engineering','project':'order_service','prompt':'review fixture'}
        spec=engineering.manifest('order_service',include_reference=True)
        with patch.object(console.model_service,'complete',return_value=LLMResponse(response_message=json.dumps({'files':spec['reference_fix_files']}),usage={'prompt_tokens':100,'completion_tokens':50})):
            turn=console.conversations.submit(value)
        return value,turn
    def test_unconfigured_model_must_not_silently_use_reference_answer(self):
        with TemporaryDirectory() as folder,AIOSKernelRuntime(storage_root=str(Path(folder)/'store')) as kernel:
            console=self.fixture(kernel,folder)
            try:
                self.assertFalse(console.model_service.request('status')['configured'])
                with self.assertRaises(ValueError):
                    console.submit({'preset':'engineering','project':'order_service','prompt':'repair without an explicitly selected test mode'})
            finally:console.close()
    def test_cgroup_enabled_console_constructs(self):
        with TemporaryDirectory() as folder,AIOSKernelRuntime(storage_root=str(Path(folder)/'store')) as kernel:
            kernel.cgroup_manager.enabled=True;kernel.cgroup_manager.cgroup_root=folder
            console=self.fixture(kernel,folder)
            console.close()
    def test_per_job_telemetry_does_not_include_previous_workflow_usage(self):
        with TemporaryDirectory() as folder,AIOSKernelRuntime(storage_root=str(Path(folder)/'store')) as kernel:
            console=self.fixture(kernel,folder)
            try:
                _,first=self.run_good(console);_,second=self.run_good(console)
                a=console.detail(first['job_id'])['agent_telemetry']['patcher_agent']['model_calls_used']
                b=console.detail(second['job_id'])['agent_telemetry']['patcher_agent']['model_calls_used']
                self.assertEqual(a,1);self.assertEqual(b,1,'Second job telemetry contains cumulative first-job calls')
            finally:console.close()
    def test_same_request_different_project_is_rejected(self):
        with TemporaryDirectory() as folder,AIOSKernelRuntime(storage_root=str(Path(folder)/'store')) as kernel:
            console=self.fixture(kernel,folder)
            try:
                value,turn=self.run_good(console)
                with self.assertRaises(ValueError):console.conversations.submit({**value,'project':'pricing'})
            finally:console.close()
    def test_download_rechecks_committed_zip_hash(self):
        with TemporaryDirectory() as folder,AIOSKernelRuntime(storage_root=str(Path(folder)/'store')) as kernel:
            console=self.fixture(kernel,folder)
            try:
                _,turn=self.run_good(console)
                (console.root/(turn['job_id']+'_delivery.zip')).write_bytes(b'corrupted archive')
                with self.assertRaises(ValueError):console.download_zip(turn['job_id'])
            finally:console.close()

if __name__=='__main__':unittest.main()
