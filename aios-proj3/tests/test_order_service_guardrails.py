"""
Automated unit tests locking the multi-file `order_service` repair scenario,
kernel-level Multi-Agent ACB/PID/cgroup/Token/ctx_ref isolation, non-cascading
audit branch fault isolation, and all 6 anti-fake-fix guardrails.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from aios.tool import engineering
from cerebrum.llm.apis import LLMResponse
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole


class OrderServiceAndGuardrailsTest(unittest.TestCase):
    def test_guardrail_1_hidden_tests_and_reference_fix_never_enter_model_context(self):
        """Guardrail #1: Reference fix and hidden tests (`tests/test_hidden.py`) are strictly excluded from model prompts."""
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / "store"), log_mode="silent") as kernel:
            console = NativeConsole(kernel, Path(folder) / "jobs")
            try:
                spec_full = engineering.manifest("order_service", include_reference=True)
                hidden_test_snippet = "TestOrderServiceHidden"
                hidden_func_snippet = "test_shipped_full_return_excludes_shipping_and_caps_remaining"

                observed_prompts = []

                def intercept_model(query):
                    serialized = json.dumps(query.messages, ensure_ascii=False)
                    observed_prompts.append(serialized)
                    self.assertNotIn("reference_fix", serialized)
                    self.assertNotIn("test_hidden", serialized)
                    self.assertNotIn(hidden_test_snippet, serialized)
                    self.assertNotIn(hidden_func_snippet, serialized)
                    return LLMResponse(
                        response_message=json.dumps({"files": spec_full["reference_fix_files"]}, ensure_ascii=False),
                        usage={"prompt_tokens": 320, "completion_tokens": 180, "total_tokens": 500},
                    )

                with patch.object(console.model_service, "complete", side_effect=intercept_model):
                    turn = console.conversations.submit({
                        "conversation_id": uuid4().hex,
                        "request_id": uuid4().hex,
                        "prompt": "repair order_service",
                        "mode": "engineering",
                        "project": "order_service",
                        "include_audit_branch": True,
                    })

                self.assertEqual(len(observed_prompts), 1)
                self.assertEqual(turn["status"], "succeeded")
                ans = json.loads(turn["answer"])
                self.assertTrue(ans["success"])
            finally:
                console.close()

    def test_guardrail_2_test_files_cannot_be_modified_or_deleted(self):
        """Guardrail #2: Patch whitelist rejects touching `tests/`, and pre/post test SHA-256 verification catches tampering."""
        spec = engineering.manifest("order_service")
        bad_patch_1 = {
            "files": {
                **spec["reference_fix_files"],
                "tests/test_public.py": "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n",
            }
        }
        with self.assertRaisesRegex(ValueError, "Guardrail violation"):
            engineering.validate_multifile_patch(bad_patch_1, "order_service")

        bad_patch_2 = {
            "files": {
                "pricing.py": "",
                "shipping.py": spec["reference_fix_files"]["shipping.py"],
                "refunds.py": spec["reference_fix_files"]["refunds.py"],
            }
        }
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            engineering.validate_multifile_patch(bad_patch_2, "order_service")

    def test_guardrail_3_zero_tests_all_skipped_or_timeout_are_rejected(self):
        """Guardrail #3: Zero tests run, all tests skipped, failures/errors > 0, or subprocess timeout all count as failure."""
        with TemporaryDirectory() as ws:
            ws_path = Path(ws)
            tests_dir = ws_path / "tests"
            tests_dir.mkdir(parents=True, exist_ok=True)

            # Case A: 0 tests run
            (tests_dir / "test_zero.py").write_text("import unittest\nclass Empty(unittest.TestCase):\n    pass\n", encoding="utf-8")
            res_zero = engineering._run_unittest_file_in_subprocess(ws_path, "tests/test_zero.py")
            self.assertEqual(res_zero["tests_run"], 0)
            self.assertFalse(res_zero["success"])

            # Case B: All tests skipped
            (tests_dir / "test_skip.py").write_text(
                "import unittest\nclass Skipped(unittest.TestCase):\n    @unittest.skip('skip all')\n    def test_1(self): pass\n",
                encoding="utf-8",
            )
            res_skip = engineering._run_unittest_file_in_subprocess(ws_path, "tests/test_skip.py")
            self.assertEqual(res_skip["tests_run"], 1)
            self.assertEqual(res_skip["skipped"], 1)
            self.assertFalse(res_skip["success"])

            # Case C: Timeout
            (tests_dir / "test_hang.py").write_text(
                "import time, unittest\nclass Hang(unittest.TestCase):\n    def test_hang(self):\n        time.sleep(2.0)\n",
                encoding="utf-8",
            )
            res_timeout = engineering._run_unittest_file_in_subprocess(ws_path, "tests/test_hang.py", timeout=0.3)
            self.assertTrue(res_timeout["timed_out"])
            self.assertFalse(res_timeout["success"])

    def test_guardrail_4_and_5_disk_execution_and_zip_sha256_consistency(self):
        """Guardrail #4 & #5: Patches are written to disk files before subprocess tests, and downloaded ZIP hashes match verified workspace."""
        spec = engineering.manifest("order_service")
        with TemporaryDirectory() as ws:
            ws_path = Path(ws)
            checked = engineering.run_multifile_check(spec["reference_fix_files"], "order_service", workspace_dir=ws_path)
            self.assertTrue(checked["success"])
            self.assertTrue(checked["workspace_written"])
            for fname in ("pricing.py", "shipping.py", "refunds.py"):
                disk_file = ws_path / "order_service" / fname
                self.assertTrue(disk_file.is_file())
                self.assertEqual(disk_file.read_text(encoding="utf-8"), spec["reference_fix_files"][fname])

            zip_bytes, meta = engineering.build_delivery_zip_bytes(checked, "wf_guardrail_5")
            verified = engineering.verify_delivery_zip_bytes(zip_bytes, checked["source_sha256"], multi_file=True)
            self.assertTrue(verified["verified"])
            self.assertEqual(verified["source_tree_sha256"], checked["source_sha256"])

            # Tampering with expected hash must raise ValueError
            with self.assertRaisesRegex(ValueError, "Guardrail #5 mismatch"):
                engineering.verify_delivery_zip_bytes(zip_bytes, "0" * 64, multi_file=True)

    def test_guardrail_6_and_multi_agent_observability_and_fault_isolation(self):
        """
        Guardrail #6 & Multi-Agent System View:
        - Idempotent request_id prevents duplicate model calls or file writes
        - Distinct ACBs (`analyst_agent`, `patcher_agent`, `tester_agent`, `reviewer_agent`, `audit_agent`)
        - Non-cascading fault isolation when `audit_agent` fails (`completed_with_failures` while main repair succeeds)
        """
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / "store"), log_mode="silent") as kernel:
            console = NativeConsole(kernel, Path(folder) / "jobs")
            try:
                spec = engineering.manifest("order_service")
                call_counter = {"count": 0}

                def stub_model(query):
                    call_counter["count"] += 1
                    return LLMResponse(
                        response_message=json.dumps({"files": spec["reference_fix_files"]}, ensure_ascii=False),
                        usage={"prompt_tokens": 410, "completion_tokens": 220, "total_tokens": 630},
                    )

                conv_id = uuid4().hex
                req_id = uuid4().hex
                wf_id = uuid4().hex
                payload = {
                    "conversation_id": conv_id,
                    "request_id": req_id,
                    "prompt": "repair order_service with isolated audit fault",
                    "mode": "engineering",
                    "project": "order_service",
                    "simulate_audit_failure": True,
                }
                with patch.object(console.model_service, "complete", side_effect=stub_model):
                    first = console.conversations.submit(payload, workflow_id=wf_id)
                    # Duplicate submission with same (conversation_id, request_id) must NOT replay model or write operations
                    second = console.conversations.submit(payload, workflow_id=wf_id)

                self.assertEqual(call_counter["count"], 1)
                self.assertEqual(first["answer"], second["answer"])

                job_detail = console.detail(wf_id)
                self.assertEqual(job_detail["status"], "completed_with_failures")
                self.assertEqual(job_detail["states"]["static_audit"], "failed")
                self.assertEqual(job_detail["states"]["answer"], "succeeded")
                self.assertTrue(job_detail["engineering"]["accepted"])

                telemetry = job_detail.get("agent_telemetry", {})
                for ag_name in ("analyst_agent", "patcher_agent", "tester_agent", "reviewer_agent", "audit_agent"):
                    self.assertIn(ag_name, telemetry)
                self.assertGreater(len(telemetry["tester_agent"]["worker_pids"]), 0)
                self.assertEqual(telemetry["patcher_agent"]["model_calls_used"], 1)
                self.assertGreater(telemetry["patcher_agent"]["input_tokens_used"], 0)
                self.assertGreater(telemetry["patcher_agent"]["output_tokens_used"], 0)

                # Verify downloadable ZIP from console matches verified workspace SHA-256
                zip_bytes = console.download_zip(wf_id)
                verified_zip = engineering.verify_delivery_zip_bytes(
                    zip_bytes, job_detail["engineering"]["source_sha256"], multi_file=True
                )
                self.assertTrue(verified_zip["verified"])
            finally:
                console.close()


if __name__ == "__main__":
    unittest.main()
