import json
import os
import tempfile
import threading
import unittest
import urllib.request

from cerebrum import (
    CerebrumClient,
    LLMQuery,
    MemoryQuery,
    StorageQuery,
    ToolQuery,
)
from aios.os_kernel.cgroup_v2 import CgroupV2Manager
from aios.scheduler.drf_admission import DRFAdmissionController
from aios.syscall.llm import LLMSyscall
from runtime.launch import AIOSKernelRuntime, create_http_server


class TestAIOSFormalKernel(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_root = os.path.join(self.temp_dir.name, "lsfs_root")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_four_syscall_subsystems_and_context_injection(self) -> None:
        with AIOSKernelRuntime(
            scheduler_policy="fifo",
            storage_root=self.storage_root,
            log_mode="silent",
        ) as kernel:
            client = CerebrumClient(local_executor=kernel.execute_request, default_user_id="user_alpha")

            # 1. Memory Syscall: store user preference
            mem_res = client.memory_op(
                "memory_agent",
                "add_memory",
                {"content": "User prefers openEuler Linux with cgroup v2 isolation", "metadata": {"policy": "shared"}},
                user_id="user_alpha",
            )
            self.assertTrue(mem_res["response"].finished)
            self.assertIsNotNone(mem_res["response"].memory_id)

            # 2. LLM Syscall: verify ContextInjector injects the stored memory
            llm_res = client.llm_chat(
                "planner_agent",
                messages=[{"role": "user", "content": "What OS do I prefer?"}],
                user_id="user_alpha",
            )
            self.assertTrue(llm_res["response"].finished)
            self.assertIn("[ctx:1]", llm_res["response"].response_message)

            # 3. Tool Syscall: execute safe arithmetic via ToolManager
            tool_res = client.call_tool(
                "math_agent",
                "math/calculate",
                {"expression": "(16 + 4) * 5"},
            )
            self.assertTrue(tool_res["response"].finished)
            self.assertEqual(tool_res["response"].response_message["result"], 100)

            # 4. Storage Syscall (LSFS): write, update, rollback, and retrieve
            client.storage_op(
                "writer_agent",
                "write",
                {"file_path": "docs/arch.md", "content": "Version 1: Initial AIOS Kernel"},
            )
            client.storage_op(
                "writer_agent",
                "write",
                {"file_path": "docs/arch.md", "content": "Version 2: Draft with bug"},
            )
            rb_res = client.storage_op(
                "writer_agent",
                "rollback",
                {"file_path": "docs/arch.md", "n": 1},
            )
            self.assertEqual(rb_res["response"].response_message, "Version 1: Initial AIOS Kernel")

            ret_res = client.storage_op(
                "reader_agent",
                "retrieve",
                {"query_text": "AIOS Kernel", "k": 2},
            )
            parsed = json.loads(ret_res["response"].response_message)
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0]["file_path"], "docs/arch.md")

    def test_lsfs_blocks_path_traversal(self) -> None:
        with AIOSKernelRuntime(storage_root=self.storage_root, log_mode="silent") as kernel:
            client = CerebrumClient(local_executor=kernel.execute_request)
            res = client.storage_op(
                "malicious_agent",
                "read",
                {"file_path": "../../outside.txt"},
            )
            self.assertEqual(res["response"].status_code, 500)
            self.assertIn("Path traversal", res["response"].error or "")

    def test_memory_lruk_eviction_and_write_barrier(self) -> None:
        # Set small memory limit (120 bytes) to trigger LRU-K eviction
        with AIOSKernelRuntime(
            storage_root=self.storage_root,
            memory_limit=120,
            eviction_k=2,
            log_mode="silent",
        ) as kernel:
            mm = kernel.memory_manager
            n1 = mm.add_memory("A" * 50, user_id="u1")
            n2 = mm.add_memory("B" * 50, user_id="u1")
            # Touch n1 twice so its LRU-2 score is higher than n2
            mm.get_memory(n1.memory_id)
            mm.get_memory(n1.memory_id)
            # Adding n3 (50 bytes) exceeds 120 bytes -> n2 must be evicted, n1 retained
            n3 = mm.add_memory("C" * 50, user_id="u1")
            self.assertIsNotNone(mm.get_memory(n1.memory_id))
            self.assertIsNone(mm.get_memory(n2.memory_id))
            self.assertIsNotNone(mm.get_memory(n3.memory_id))

            # Verify WriteBarrier blocks until pending write completes
            token, seq = mm.barrier.register_write("u_barrier")
            released = []

            def delayed_complete() -> None:
                released.append("completed")
                mm.barrier.complete_write("u_barrier", token)

            timer = threading.Timer(0.05, delayed_complete)
            timer.start()
            ok = mm.barrier.wait_for_seq("u_barrier", seq)
            timer.join()
            self.assertTrue(ok)
            self.assertEqual(released, ["completed"])

    def test_rr_scheduler_context_switch_and_resumption(self) -> None:
        with AIOSKernelRuntime(
            scheduler_policy="rr",
            storage_root=self.storage_root,
            time_slice=0.03,
            log_mode="silent",
        ) as kernel:
            query = LLMQuery(
                messages=[{"role": "user", "content": "one two three four five six"}],
            )
            # Attach per-token delay so generation spans multiple 30ms RR time slices
            setattr(query, "per_token_delay", 0.015)
            result = kernel.execute_request("sliced_agent", query)
            self.assertTrue(result["response"].finished)
            self.assertGreater(len(result["start_times"]), 1)
            self.assertIn("Processed: one two three four five six", result["response"].response_message)

    def test_drf_fairness_and_cgroup_v2_controller(self) -> None:
        drf = DRFAdmissionController(cpu_milli_budget=2000, memory_bytes_budget=512 * 1024 * 1024, pids_budget=64)
        # Agent A acquires 1000m CPU (50% dominant share), Agent B has 0% share
        held = drf.acquire("agent_heavy")
        sc_heavy = LLMSyscall("agent_heavy", LLMQuery(messages=[]))
        sc_light = LLMSyscall("agent_light", LLMQuery(messages=[]))
        sc_heavy.set_created_time(100.0)
        sc_light.set_created_time(101.0)

        idx, chosen = drf.select_next_syscall([sc_heavy, sc_light])
        self.assertEqual(idx, 1)
        self.assertEqual(chosen.agent_name, "agent_light")
        drf.release("agent_heavy", held)

        # Test CgroupV2Manager file writing with simulated cgroup directory
        cg_root = os.path.join(self.temp_dir.name, "cgroup_mock")
        os.makedirs(cg_root, exist_ok=True)
        cg = CgroupV2Manager(cgroup_root=cg_root, strict=False)
        cg.enabled = True  # Force-enable on any OS against the mock directory
        agent_dir = os.path.join(cg_root, "agent_worker_1", "syscall_7")
        os.makedirs(agent_dir, exist_ok=True)
        with open(os.path.join(agent_dir, "..", "cgroup.subtree_control"), "w", encoding="utf-8"):
            pass
        for fname in ("cpu.max", "memory.max", "memory.swap.max", "pids.max", "cgroup.kill"):
            with open(os.path.join(agent_dir, fname), "w", encoding="utf-8") as f:
                f.write("")

        slice_info = cg.create_syscall_slice("worker_1", 7, cpu_milli=500, memory_bytes=134217728, pids_max=16)
        self.assertEqual(slice_info["mode"], "cgroup_v2_enforced")
        with open(os.path.join(agent_dir, "cpu.max"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "50000 100000")
        with open(os.path.join(agent_dir, "memory.swap.max"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "0")

    def test_remote_kernel_http_server_and_cerebrum_client(self) -> None:
        with AIOSKernelRuntime(
            scheduler_policy="fifo",
            storage_root=self.storage_root,
            log_mode="silent",
        ) as kernel:
            remote_client = CerebrumClient(
                rpc_handler=kernel.handle_query_payload,
                default_user_id="remote_u1",
            )

            # Execute remote ToolQuery over JSON RPC payload protocol (/query)
            tool_out = remote_client.call_tool(
                "remote_agent",
                "math/calculate",
                {"expression": "7 * 8 + 4"},
            )
            self.assertEqual(tool_out["response"]["response_message"]["result"], 60)

            # Execute remote LLMQuery over JSON RPC payload protocol (/query)
            llm_out = remote_client.llm_chat(
                "remote_agent",
                messages=[{"role": "user", "content": "Hello Remote AIOS Kernel"}],
            )
            self.assertIn("Hello Remote AIOS Kernel", llm_out["response"]["response_message"])

            # Inspect status_snapshot and syscall history ledger
            status = kernel.status_snapshot()
            self.assertEqual(status["kernel"], "AIOS")
            self.assertTrue(status["active"])

            syscalls = kernel.syscall_executor.get_history(limit=100)
            self.assertGreaterEqual(len(syscalls), 2)


if __name__ == "__main__":
    unittest.main()
