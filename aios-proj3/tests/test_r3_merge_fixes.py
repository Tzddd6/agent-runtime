"""Independent review; fixed synthetic responses, no supplier calls."""
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from tests import test_independent_delivery_review as original_review


class NewBoundaries(unittest.TestCase):
    def run_case(self, callback):
        from runtime.launch import AIOSKernelRuntime
        from runtime.web import NativeConsole
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder)/'store')) as kernel:
            console=NativeConsole(kernel,Path(folder)/'jobs')
            try:
                return callback(kernel,console)
            finally:
                console.close()

    def test_download_contains_public_tests(self):
        def check(kernel,console):
            _,turn=original_review.IndependentDeliveryReview().run_good(console)
            with ZipFile(io.BytesIO(console.download_zip(turn['job_id']))) as z:
                self.assertIn('tests/test_public.py',z.namelist())
        self.run_case(check)

    def test_full_receipt_buffer_preserves_current_job_workers(self):
        def check(kernel,console):
            # Receipts are explicitly capped at 100; seed historical receipts.
            kernel.process_service.receipts[:]=[{'id':str(n),'agent':'historical'} for n in range(100)]
            _,turn=original_review.IndependentDeliveryReview().run_good(console)
            info=console.detail(turn['job_id'])['agent_telemetry']['tester_agent']
            self.assertTrue(info['worker_pids'],'Current real Workers disappear when receipt ring is full')
            self.assertNotEqual(info['enforcement'],'in_process_role')
        self.run_case(check)

    def test_job_detail_is_json_serializable(self):
        def check(kernel,console):
            _,turn=original_review.IndependentDeliveryReview().run_good(console)
            json.dumps(console.detail(turn['job_id']))
        self.run_case(check)

    def test_benchmark_identical_prompt_messages(self):
        import tools.benchmark_interleaved_runtime_vs_direct as bench
        from runtime.launch import AIOSKernelRuntime
        from cerebrum.llm.apis import LLMQuery
        runtime_messages=[];direct_messages=[]
        original_execute=AIOSKernelRuntime.execute_request
        original_direct=bench._direct_llm_call
        def capture_execute(kernel,agent,query):
            if isinstance(query,LLMQuery):
                runtime_messages.append(query.messages)
            return original_execute(kernel,agent,query)
        def capture_direct(messages,*args,**kwargs):
            direct_messages.append(messages)
            return original_direct(messages,*args,**kwargs)
        with patch.object(bench,'BENCH_PROJECTS',('order_service',)),patch.object(AIOSKernelRuntime,'execute_request',capture_execute),patch.object(bench,'_direct_llm_call',capture_direct):
            bench.run_one_arm_round('A',1,use_stub=True)
            bench.run_one_arm_round('B',1,use_stub=True)
        Path('captured-benchmark-prompts.json').write_text(json.dumps({'runtime':runtime_messages,'direct':direct_messages},ensure_ascii=False,indent=2),encoding='utf-8')
        self.assertEqual(len(runtime_messages),1)
        self.assertEqual(runtime_messages,direct_messages,'Control arms receive different model input')

    def test_download_retests_in_empty_directory(self):
        import os, subprocess, sys
        def check(kernel,console):
            _,turn=original_review.IndependentDeliveryReview().run_good(console)
            with TemporaryDirectory() as folder:
                with ZipFile(io.BytesIO(console.download_zip(turn['job_id']))) as z:
                    z.extractall(folder)
                env={"PATH":os.defpath,"PYTHONPATH":str(Path(folder)/'src'),"PYTHONDONTWRITEBYTECODE":"1"}
                if os.name=='nt':env['SystemRoot']=os.environ['SystemRoot']
                run=subprocess.run([sys.executable,'-B','-m','unittest','discover','-s','tests','-v'],
                    cwd=folder,env=env,capture_output=True,text=True,encoding='utf-8',timeout=20)
                self.assertEqual(run.returncode,0,run.stderr)
                self.assertRegex(run.stderr,r'Ran [1-9][0-9]* tests')
        self.run_case(check)

    def test_second_round_same_inputs_same_repair_opportunity(self):
        import tools.benchmark_interleaved_runtime_vs_direct as bench
        with patch.object(bench,'BENCH_PROJECTS',('order_service',)):
            runtime=bench.run_one_arm_round('A',1,fail_first=True)
            direct=bench.run_one_arm_round('B',1,fail_first=True)
        self.assertEqual(runtime['status'],'succeeded',runtime.get('error_type'))
        self.assertEqual(direct['status'],'succeeded',direct.get('error_type'))
        self.assertEqual(runtime['model_calls'],2)
        self.assertEqual(direct['model_calls'],2)
        self.assertEqual([c['input_sha256'] for c in runtime['projects'][0]['calls']],
                         [c['input_sha256'] for c in direct['projects'][0]['calls']])

    def test_receipt_capture_unsubscribes_after_exception(self):
        def check(kernel,console):
            with self.assertRaises(ValueError):
                with kernel.process_service.capture_receipts():
                    raise ValueError('injected')
            self.assertEqual(kernel.process_service._receipt_captures,{})
        self.run_case(check)

    def test_concurrency_flag_cannot_lie(self):
        import tools.benchmark_interleaved_runtime_vs_direct as bench
        with self.assertRaises(ValueError):
            bench.run_one_arm_round('A',1,concurrency=2)

    def test_uncertain_provider_failure_is_recorded_once_per_arm(self):
        import tools.benchmark_interleaved_runtime_vs_direct as bench
        with patch.object(bench,'BENCH_PROJECTS',('order_service',)), patch.object(
                bench,'_provider_call',side_effect=TimeoutError('synthetic response lost')):
            rows=[bench.run_one_arm_round(arm,1) for arm in ('A','B')]
        for row in rows:
            self.assertEqual(row['status'],'failed')
            self.assertEqual(row['model_calls'],1)
            self.assertIsNone(row['input_tokens'])
            self.assertEqual(row['projects'][0]['calls'][0]['error_type'],'TimeoutError')
