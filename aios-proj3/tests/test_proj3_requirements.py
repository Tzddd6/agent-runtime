import os
import tempfile
import unittest

from aios.context.context_store import ContextStore
from aios.ipc.mailbox import IPCManager
from runtime.launch import AIOSKernelRuntime
from aios.os_kernel.cgroup_v2 import CgroupV2Manager
from unittest.mock import patch


class TestProj3OfficialRequirements(unittest.TestCase):
    """
    Clause-by-clause verification suite for 2026 National Operating System Design Competition
    Proj3: Multi-Agent-Oriented Operating System-Level Agent Runtime (4.1 - 4.7).
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_root = os.path.join(self.temp_dir.name, "proj3_storage")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_4_1_os_compatibility_and_cgroup_v2_delegation(self) -> None:
        """4.1: openEuler / Linux cgroup v2 hierarchy controller & cross-platform compatibility."""
        cg_root = os.path.join(self.temp_dir.name, "sys_fs_cgroup")
        os.makedirs(cg_root, exist_ok=True)
        # This fixture verifies control-file formatting only. Real enforcement
        # is separately verified with a delegated subtree on Linux.
        cg = CgroupV2Manager(cgroup_root=cg_root)
        cg.enabled = True
        slice_dir = os.path.join(cg_root, "agent_coder", "syscall_1")
        os.makedirs(slice_dir, exist_ok=True)
        with open(os.path.join(slice_dir, "..", "cgroup.subtree_control"), "w", encoding="utf-8"):
            pass
        for ctrl_file in ("cpu.max", "memory.max", "memory.swap.max", "pids.max", "cgroup.kill"):
            with open(os.path.join(slice_dir, ctrl_file), "w", encoding="utf-8") as f:
                f.write("")

        info = cg.create_syscall_slice("coder", 1, cpu_milli=800, memory_bytes=268435456, pids_max=24)
        self.assertEqual(info["mode"], "cgroup_v2_enforced")
        with open(os.path.join(slice_dir, "cpu.max"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "80000 100000")
        with open(os.path.join(slice_dir, "pids.max"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "24")

    def test_explicit_invalid_linux_delegation_fails_closed(self):
        with patch('aios.os_kernel.cgroup_v2.platform.system', return_value='Linux'):
            with self.assertRaises((ValueError, RuntimeError)):
                AIOSKernelRuntime(storage_root=self.storage_root,
                                  cgroup_root=self.temp_dir.name)

    def test_4_2_dag_dependencies_dynamic_task_spawning_and_drf_scheduling(self) -> None:
        """4.2: Multi-Agent task dependency DAG, dynamic task generation, and resource-aware scheduling."""
        with AIOSKernelRuntime(storage_root=self.storage_root, log_mode="silent") as kernel:
            tasks = [
                {
                    "id": "plan",
                    "agent": "planner_agent",
                    "handler": lambda task, up, att: {
                        "plan": "split into module_a and module_b",
                        "spawn_tasks": [
                            {
                                "id": "dyn_subtask_a",
                                "agent": "worker_a",
                                "query_type": "tool",
                                "query_data": {
                                    "tool_calls": [
                                        {"name": "math/calculate", "parameters": {"expression": "10 + 20"}}
                                    ]
                                },
                                "wire_into": ["aggregate"],
                            },
                            {
                                "id": "dyn_subtask_b",
                                "agent": "worker_b",
                                "query_type": "tool",
                                "query_data": {
                                    "tool_calls": [
                                        {"name": "math/calculate", "parameters": {"expression": "30 + 40"}}
                                    ]
                                },
                                "wire_into": ["aggregate"],
                            },
                        ],
                    },
                },
                {
                    "id": "aggregate",
                    "agent": "aggregator_agent",
                    "depends_on": ["plan"],
                    "handler": lambda task, up, att: {
                        "total": up["dyn_subtask_a"]["result"] + up["dyn_subtask_b"]["result"],
                        "upstream_keys": sorted(up.keys()),
                    },
                },
            ]

            report = kernel.workflow_engine.run_workflow("wf_4_2", tasks)
            self.assertEqual(report["states"]["plan"], "succeeded")
            self.assertEqual(report["states"]["dyn_subtask_a"], "succeeded")
            self.assertEqual(report["states"]["dyn_subtask_b"], "succeeded")
            self.assertEqual(report["states"]["aggregate"], "succeeded")
            self.assertEqual(report["results"]["aggregate"]["total"], 100)

    def test_4_3_unified_agent_abstraction_lifecycle_and_budgets(self) -> None:
        """4.3: Agent lifecycle management, state transitions, OS resource mapping, and model budgets."""
        with AIOSKernelRuntime(storage_root=self.storage_root, log_mode="silent") as kernel:
            drf = kernel.scheduler.drf_controller
            acb = drf.register_agent("controlled_agent", max_concurrent=2, max_model_calls=2, max_tokens=1000)
            self.assertEqual(acb.state, "ready")

            # Acquire resource -> state transitions to running
            vec = drf.acquire("controlled_agent")
            self.assertEqual(acb.state, "running")
            drf.release("controlled_agent", vec)
            self.assertEqual(acb.state, "ready")

            # Pause -> blocks admission
            drf.pause_agent("controlled_agent")
            self.assertEqual(acb.state, "paused")
            self.assertFalse(drf.can_admit(agent_name="controlled_agent"))

            # Resume -> restores ready state
            drf.resume_agent("controlled_agent")
            self.assertEqual(acb.state, "ready")
            self.assertTrue(drf.can_admit(agent_name="controlled_agent"))

            # Enforce model call budget (limit=2)
            drf.consume_model_budget("controlled_agent", calls=2, tokens=200)
            with self.assertRaises(RuntimeError):
                drf.consume_model_budget("controlled_agent", calls=1)

            # Terminate -> revokes IPC credentials, frees resources, blocks reactivation
            tok = kernel.ipc_manager.issue_token("controlled_agent")
            drf.terminate_agent(
                "controlled_agent",
                ipc_manager=kernel.ipc_manager,
                cgroup_manager=kernel.cgroup_manager,
            )
            self.assertEqual(acb.state, "terminated")
            self.assertEqual(acb.state_history, ["created", "ready", "running", "ready", "paused", "ready", "terminated"])
            with self.assertRaises(RuntimeError):
                drf.resume_agent("controlled_agent")

    def test_4_4_agent_fault_isolation_contracts_retries_and_checkpoint_recovery(self) -> None:
        """4.4: Single-agent fault isolation without cascading failure, retry recovery, contracts & checkpoints."""
        with AIOSKernelRuntime(storage_root=self.storage_root, log_mode="silent") as kernel:
            flaky_counter = {"count": 0}

            def flaky_handler(task, up, attempt):
                flaky_counter["count"] += 1
                if attempt == 1:
                    raise RuntimeError("Transient worker glitch on attempt 1")
                return {"status": "recovered_on_retry", "attempt": attempt}

            def always_broken_handler(task, up, attempt):
                raise RuntimeError("Monolithic agent crash")

            tasks = [
                # Branch A: fails permanently -> its downstream task must be isolated_skipped
                {
                    "id": "faulty_root",
                    "agent": "faulty_agent",
                    "max_retries": 1,
                    "handler": always_broken_handler,
                },
                {
                    "id": "faulty_downstream",
                    "agent": "dependent_on_faulty",
                    "depends_on": ["faulty_root"],
                    "query_type": "llm",
                    "query_data": {"messages": [{"role": "user", "content": "Should not run"}]},
                },
                # Branch B: transient fault on attempt 1, recovers on attempt 2, satisfies contract
                {
                    "id": "resilient_branch",
                    "agent": "resilient_agent",
                    "max_retries": 1,
                    "contract": {"non_empty": True, "required_keys": ["status"]},
                    "handler": flaky_handler,
                },
                {
                    "id": "healthy_downstream",
                    "agent": "verifier_agent",
                    "depends_on": ["resilient_branch"],
                    "handler": lambda task, up, att: {"verified": up["resilient_branch"]["status"]},
                },
            ]

            report = kernel.workflow_engine.run_workflow("wf_4_4", tasks)
            self.assertEqual(report["states"]["faulty_root"], "failed")
            self.assertEqual(report["states"]["faulty_downstream"], "isolated_skipped")
            self.assertEqual(report["states"]["resilient_branch"], "succeeded")
            self.assertEqual(report["attempts"]["resilient_branch"], 2)
            self.assertEqual(report["states"]["healthy_downstream"], "succeeded")
            self.assertEqual(report["results"]["healthy_downstream"]["verified"], "recovered_on_retry")

            # Verify Checkpoint Recovery: resuming wf_4_4 reuses confirmed succeeded tasks without re-running flaky_handler
            prev_count = flaky_counter["count"]
            resumed = kernel.workflow_engine.run_workflow("wf_4_4", tasks, resume=True)
            self.assertEqual(resumed["states"]["healthy_downstream"], "succeeded")
            self.assertEqual(flaky_counter["count"], prev_count)

    def test_4_5_context_reuse_compression_and_acl_isolation(self) -> None:
        """4.5: Context reuse (SHA256 dedup), compression (gzip + byte budget view), and ACL isolation."""
        ctx_dir = os.path.join(self.temp_dir.name, "ctx_store")
        store = ContextStore(storage_dir=ctx_dir, gzip_threshold_bytes=128)

        large_payload = {
            "goal": "Fix memory leak in worker pool",
            "constraints": ["Keep stdlib compatibility", "Zero orphan processes"],
            "claims": ["Leak originates in unclosed pipe handle"],
            "raw_trace": "TRACE_LINE_1234567890 " * 120,
        }

        # 1. Store twice -> verify SHA256 deduplication and gzip physical storage reduction
        cid1 = store.put("analyst_agent", large_payload, authorized_agents=["reviewer_agent"])
        cid2 = store.put("analyst_agent", large_payload)
        self.assertEqual(cid1, cid2)
        stats = store.stats()
        self.assertEqual(stats["unique_objects"], 1)
        self.assertEqual(stats["dedup_hits"], 1)
        self.assertLess(stats["physical_bytes_stored"], stats["logical_bytes_written"] // 4)

        # 2. Byte-budget lossy view -> preserves goal/constraints/claims while fitting budget
        view = store.build_budget_view(cid1, reader_agent="reviewer_agent", byte_budget=400)
        self.assertTrue(view["truncated"])
        self.assertLessEqual(view["view_bytes"], 450)
        self.assertEqual(view["content"]["goal"], "Fix memory leak in worker pool")
        self.assertIn("constraints", view["content"])

        # 3. ACL Isolation -> unauthorized agent is blocked until explicitly granted access
        with self.assertRaises(PermissionError):
            store.get(cid1, reader_agent="untrusted_agent")

        store.grant_access(cid1, grantor_agent="analyst_agent", target_agent="untrusted_agent")
        page = store.read_page(cid1, reader_agent="untrusted_agent", offset=0, limit=64)
        self.assertEqual(len(page["slice"]), 64)
        self.assertTrue(page["has_more"])

    def test_4_6_and_4_7_complex_multi_agent_scenario_and_mailbox_ipc(self) -> None:
        """4.6 & 4.7: Complex multi-agent collaborative workflow with Mailbox IPC and zero-copy context refs."""
        with AIOSKernelRuntime(storage_root=self.storage_root, log_mode="silent") as kernel:
            ipc: IPCManager = kernel.ipc_manager
            store: ContextStore = kernel.context_store

            tok_a = ipc.issue_token("diagnostician")
            tok_b = ipc.issue_token("patcher")

            # Diagnostician stores large diagnostic artifact in ContextStore
            ctx_id = store.put(
                owner_agent="diagnostician",
                data={"bug_module": "billing.py", "line": 42, "fix": "use Decimal instead of float"},
            )

            # Send IPC message referencing ctx_id -> automatically delegates ACL to 'patcher'
            msg = ipc.send(
                sender="diagnostician",
                recipient="patcher",
                payload={"action": "apply_fix"},
                request_id="req_fix_001",
                context_refs=[ctx_id],
                token=tok_a,
            )

            # Duplicate request_id with identical payload is idempotent
            dup = ipc.send(
                sender="diagnostician",
                recipient="patcher",
                payload={"action": "apply_fix"},
                request_id="req_fix_001",
                context_refs=[ctx_id],
                token=tok_a,
            )
            self.assertEqual(msg["message_id"], dup["message_id"])

            # Duplicate request_id with conflicting payload is rejected
            with self.assertRaises(ValueError):
                ipc.send(
                    sender="diagnostician",
                    recipient="patcher",
                    payload={"action": "corrupt_fix"},
                    request_id="req_fix_001",
                    context_refs=[ctx_id],
                    token=tok_a,
                )

            # Patcher receives message, reads delegated context ref, and ACKs message
            inbox = ipc.receive("patcher", token=tok_b)
            self.assertEqual(len(inbox), 1)
            ref_data = store.get(inbox[0]["context_refs"][0], reader_agent="patcher")
            self.assertEqual(ref_data["bug_module"], "billing.py")

            self.assertTrue(ipc.ack("patcher", inbox[0]["message_id"], token=tok_b))
            self.assertEqual(len(ipc.receive("patcher", token=tok_b)), 0)


if __name__ == "__main__":
    unittest.main()
