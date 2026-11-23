import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cerebrum import CerebrumClient, LLMQuery
from runtime.launch import AIOSKernelRuntime


def run_demo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        storage_dir = os.path.join(tmp, "aios_lsfs")
        print("=== 1. Booting AIOS Formal Kernel (Policy: Round-Robin + DRF) ===")
        with AIOSKernelRuntime(
            scheduler_policy="rr",
            storage_root=storage_dir,
            time_slice=0.03,
            log_mode="silent",
        ) as kernel:
            client = CerebrumClient(
                rpc_handler=kernel.handle_query_payload,
                default_user_id="os_contest_team",
            )

            print("\n=== 2. Memory Syscall + Write Barrier ===")
            m_res = client.memory_op(
                "memory_agent",
                "add_memory",
                {
                    "content": "Target deployment platform is openEuler 24.03 LTS with cgroup v2 delegation.",
                    "keywords": ["openEuler", "cgroup"],
                },
            )
            print("Memory Added:", m_res["response"]["response_message"])

            print("\n=== 3. Storage Syscall (LSFS Write + Version Rollback + Semantic Retrieve) ===")
            client.storage_op(
                "storage_agent",
                "write",
                {"file_path": "kernel_spec.md", "content": "# AIOS v1.0 Formal Spec (Verified)"},
            )
            client.storage_op(
                "storage_agent",
                "write",
                {"file_path": "kernel_spec.md", "content": "# Corrupted Draft"},
            )
            rb = client.storage_op("storage_agent", "rollback", {"file_path": "kernel_spec.md", "n": 1})
            print("After LSFS Rollback(n=1):", rb["response"]["response_message"])

            print("\n=== 4. Tool Syscall (Safe Math & System Info) ===")
            t_res = client.call_tool("tool_agent", "math/calculate", {"expression": "(256 * 4) + 1024"})
            print("Tool Output:", t_res["response"]["response_message"])

            print("\n=== 5. Time-Sliced LLM Syscall with Automatic Memory Context Injection ===")
            slow_query = LLMQuery(
                messages=[{"role": "user", "content": "Summarize our openEuler cgroup deployment plan"}],
                user_id="os_contest_team",
            )
            setattr(slow_query, "per_token_delay", 0.012)
            raw_res = kernel.execute_request("planner_agent", slow_query)
            print("LLM Output:", raw_res["response"].response_message)
            print("RR Time Slices Used:", len(raw_res["start_times"]))

            print("\n=== 6. Kernel Status & Syscall Ledger Snapshot ===")
            status = kernel.status_snapshot()
            print(json.dumps(status, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    run_demo()
