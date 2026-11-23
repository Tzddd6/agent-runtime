from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from unittest.mock import patch

from aios.llm_core.profiles import ModelProfileBroker
from cerebrum import CerebrumClient
from cerebrum.llm.apis import LLMQuery
from runtime.launch import AIOSKernelRuntime


class KernelBoundariesTest(TestCase):
    def test_live_graph_and_duplicate_workflow_guard(self):
        started, release = Event(), Event()

        def handler(*_):
            started.set()
            if not release.wait(timeout=3):
                raise TimeoutError("test handler did not release")
            return {"ok": True}

        with (TemporaryDirectory() as directory,
              AIOSKernelRuntime(storage_root=str(Path(directory) / "storage")) as kernel,
              ThreadPoolExecutor(max_workers=1) as workers):
            engine = kernel.workflow_engine
            tasks = [{"id": "step", "handler": handler}]
            workflow = workers.submit(engine.run_workflow, "live-run", tasks)
            try:
                self.assertTrue(started.wait(timeout=2))
                self.assertEqual(engine.recent_graphs()[0]["nodes"][0]["state"], "running")
                with self.assertRaisesRegex(ValueError, "already running"):
                    engine.run_workflow("live-run", tasks)
            finally:
                release.set()
            self.assertEqual(workflow.result(timeout=3)["states"]["step"], "succeeded")
            self.assertEqual(engine.recent_graphs()[0]["nodes"][0]["state"], "succeeded")

    def test_two_kernels_isolate_files_and_syscall_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (AIOSKernelRuntime(storage_root=str(root / "first")) as first,
                  AIOSKernelRuntime(storage_root=str(root / "second")) as second,
                  ThreadPoolExecutor(max_workers=2) as workers):
                def write(kernel, value):
                    client = CerebrumClient(local_executor=kernel.execute_request)
                    return client.storage_op(value, "write", {"file_path": "same.txt", "content": value})

                requests = [workers.submit(write, kernel, name)
                            for kernel, name in ((first, "first"), (second, "second"))]
                for request in requests:
                    result = request.result(timeout=5)
                    self.assertEqual(result["response"].status_code, 200)
                    self.assertEqual(result["syscall_id"], 1)
                for kernel, name in ((first, "first"), (second, "second")):
                    self.assertEqual((root / name / "same.txt").read_text(), name)
                    self.assertEqual([call["agent_name"] for call in kernel.syscall_executor.get_history()], [name])

    def test_provider_credentials_are_bound_to_their_endpoint(self):
        broker = ModelProfileBroker()
        with self.assertRaises(ValueError):
            broker.save_profile({"id": "wrong_host", "provider": "openai", "label": "Wrong host",
                                 "model": "test", "key": "synthetic-fixture-only", "base_url": "https://example.com/v1"})
        with TemporaryDirectory() as directory, AIOSKernelRuntime(storage_root=str(Path(directory) / "storage")) as kernel:
            kernel.llm.profile_broker.configure_legacy("synthetic-fixture-only")
            with patch("aios.llm_core.adapter.urllib.request.urlopen") as upstream:
                result = kernel.execute_request("limited-agent", LLMQuery(model="mock-aios-core", max_new_tokens=4))
                upstream.assert_not_called()
            self.assertEqual(result["response"].status_code, 200)
            self.assertLessEqual(result["response"].usage["total_tokens"], 4)

    def test_sdk_model_requests_share_kernel_token_budget(self):
        with TemporaryDirectory() as directory, AIOSKernelRuntime(storage_root=str(Path(directory) / "storage")) as kernel:
            kernel.scheduler.drf_controller.register_agent("limited", max_tokens=10)
            query = LLMQuery(model="mock-aios-core", max_new_tokens=6)
            self.assertEqual(kernel.execute_request("limited", query)["response"].status_code, 200)
            self.assertEqual(kernel.execute_request("limited", query)["response"].status_code, 429)
            self.assertEqual(kernel.syscall_executor.get_history()[-1]["syscall_id"], 1)
