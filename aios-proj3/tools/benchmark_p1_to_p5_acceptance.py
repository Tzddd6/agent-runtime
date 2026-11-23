"""
Comprehensive Priority 1-5 Quantitative Acceptance Benchmark.

Produces `reports/p1_to_p5_acceptance.json` with reproducible before/after metrics for:
  - P1: Cloud API zero-wait queue dispatch & 3-way independent baseline
  - P2: Cold-start kernel per task vs. Resident kernel + TaskScope (sequential & concurrent + isolation)
  - P3: Naive full tool-history injection vs. ContextStore reference offloading & history compaction (token savings)
  - P4: Local schema pre-validation (0 wasted syscalls) & parallel vs. serial read-only tool execution
  - P5: Broken log pipe injection & uncertain failure zero-duplicate-retry verification
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from aios.utils.logger import _SafeStreamHandler
from cerebrum.llm.apis import LLMQuery, LLMResponse
from runtime import agent_loop
from runtime.launch import AIOSKernelRuntime
from tools.benchmark_queue_and_baseline import bench_queue_wait, bench_three_way_baseline


def bench_p2_cold_vs_resident(n_tasks: int = 8) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        # Cold-start: new kernel per task
        t0 = time.perf_counter()
        for i in range(n_tasks):
            with AIOSKernelRuntime(storage_root=str(Path(tmp) / f"cold_{i}"), log_mode="silent") as kernel:
                r = kernel.execute_request(f"c_{i}", LLMQuery(model="mock-aios-core", messages=[{"role": "user", "content": "ping"}]))
                assert r["response"].status_code == 200
        cold_wall = time.perf_counter() - t0

        # Resident kernel + TaskScope (sequential)
        with AIOSKernelRuntime(storage_root=str(Path(tmp) / "res_seq"), log_mode="silent") as kernel:
            t1 = time.perf_counter()
            for i in range(n_tasks):
                with kernel.task_scope(f"s_{i}") as scope:
                    ctx_id = scope.put_context({"task": i})
                    r = scope.execute(LLMQuery(model="mock-aios-core", messages=[{"role": "user", "content": "ping"}]))
                    assert r["response"].status_code == 200
            res_seq_wall = time.perf_counter() - t1
            seq_reclaimed = kernel.context_store.stats()["unique_objects"] == 0

        # Resident kernel + TaskScope (concurrent 8)
        with AIOSKernelRuntime(storage_root=str(Path(tmp) / "res_conc"), log_mode="silent") as kernel:
            t2 = time.perf_counter()

            def run_scoped(i: int):
                with kernel.task_scope(f"p_{i}") as scope:
                    scope.put_context({"task": i})
                    r = scope.execute(LLMQuery(model="mock-aios-core", messages=[{"role": "user", "content": "ping"}]))
                    assert r["response"].status_code == 200

            with ThreadPoolExecutor(max_workers=n_tasks) as pool:
                list(pool.map(run_scoped, range(n_tasks)))
            res_conc_wall = time.perf_counter() - t2
            conc_reclaimed = (
                kernel.context_store.stats()["unique_objects"] == 0
                and kernel.scheduler.drf_controller.used.cpu_milli == 0
            )

    return {
        "tasks": n_tasks,
        "cold_start_per_task_sec": round(cold_wall, 4),
        "resident_task_scope_seq_sec": round(res_seq_wall, 4),
        "resident_task_scope_conc_sec": round(res_conc_wall, 4),
        "speedup_resident_seq_vs_cold": round(cold_wall / max(1e-6, res_seq_wall), 2),
        "speedup_resident_conc_vs_cold": round(cold_wall / max(1e-6, res_conc_wall), 2),
        "zero_residual_after_exit": bool(seq_reclaimed and conc_reclaimed),
    }


def bench_p3_context_token_savings() -> Dict[str, Any]:
    """Compare raw full-payload multi-turn history vs. offloaded + compacted history."""
    large_payload = json.dumps({"ok": True, "result": "RECORD_DATA_0123456789 " * 300}, ensure_ascii=False)
    naive_history = [
        {"role": "system", "content": "GOAL: diagnose issue; CONSTRAINT: never drop constraints"},
        {"role": "user", "content": "inspect 4 large files"},
    ]
    for i in range(4):
        naive_history.append({"role": "tool", "tool_call_id": f"t{i}", "content": large_payload})
    naive_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in naive_history)

    # Compacted view (as used by agent_loop._compact_history_for_model after offloading)
    offloaded_history = [
        {"role": "system", "content": "GOAL: diagnose issue; CONSTRAINT: never drop constraints"},
        {"role": "user", "content": "inspect 4 large files"},
    ]
    for i in range(4):
        offloaded_history.append({
            "role": "tool",
            "tool_call_id": f"t{i}",
            "content": json.dumps({
                "ok": True,
                "context_ref": f"ctx_sha256_{i}",
                "total_chars": len(large_payload),
                "has_more": True,
                "preview": large_payload[:agent_loop.LARGE_TOOL_RESULT_CHARS],
            }, ensure_ascii=False),
        })
    compacted_view = agent_loop._compact_history_for_model(offloaded_history, keep_recent_tools=2)
    compacted_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in compacted_view)
    constraints_preserved = "CONSTRAINT: never drop constraints" in compacted_view[0]["content"]
    return {
        "scope": "synthetic_char_count_not_real_tokenizer",
        "naive_input_chars": naive_chars,
        "optimized_input_chars": compacted_chars,
        "synthetic_char_reduction_percent": round(100.0 * (1.0 - compacted_chars / max(1, naive_chars)), 2),
        "constraints_preserved": constraints_preserved,
    }


def bench_p4_validation_and_parallel_tools() -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp, AIOSKernelRuntime(storage_root=str(Path(tmp) / "store"), log_mode="silent") as kernel:
        sc_before = kernel.syscall_executor.id
        _, _, ok_bad = agent_loop._execute_single_call(
            kernel, "p4", {"id": "b1", "function": {"name": "math_calculate", "arguments": json.dumps({"bad_key": 1})}}
        )
        wasted_syscalls_on_bad_params = kernel.syscall_executor.id - sc_before

        # Compare 3 independent read-only tool calls: serial vs parallel
        calls = [
            {"id": f"r{i}", "function": {"name": "math_calculate", "arguments": json.dumps({"expression": f"{i+1}*10"})}}
            for i in range(3)
        ]
        t_ser0 = time.perf_counter()
        ser_out = [agent_loop._execute_single_call(kernel, "p4", c) for c in calls]
        ser_sec = time.perf_counter() - t_ser0

        t_par0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=3) as pool:
            par_out = list(pool.map(lambda c: agent_loop._execute_single_call(kernel, "p4", c), calls))
        par_sec = time.perf_counter() - t_par0

    return {
        "invalid_param_rejected_locally": not ok_bad,
        "wasted_kernel_syscalls_on_invalid_params": wasted_syscalls_on_bad_params,
        "serial_3_readonly_tools_sec": round(ser_sec, 4),
        "parallel_3_readonly_tools_sec": round(par_sec, 4),
        "parallel_tool_speedup": round(ser_sec / max(1e-6, par_sec), 2),
        "all_parallel_succeeded": all(o[2] for o in par_out) and all(o[2] for o in ser_out),
    }


def bench_p5_log_fault_injection() -> Dict[str, Any]:
    class _DeadPipe:
        def write(self, _):
            raise BrokenPipeError("broken")
        def flush(self):
            raise BrokenPipeError("broken")

    class _CountingFailingProvider:
        def __init__(self):
            self.calls = 0
        def complete(self, query):
            self.calls += 1
            raise RuntimeError("injected uncertain transport drop")

    handler = _SafeStreamHandler(stream=_DeadPipe())
    failing = _CountingFailingProvider()
    with tempfile.TemporaryDirectory() as tmp, AIOSKernelRuntime(storage_root=str(Path(tmp) / "store"), log_mode="console") as kernel:
        kernel.scheduler.logger._logger.handlers = [handler]
        res = kernel.execute_request("p5_resilient", LLMQuery(model="mock-aios-core", messages=[{"role": "user", "content": "survive"}]))
        ok = res["response"].status_code == 200

        kernel.llm.model_service = failing
        fail_res = kernel.execute_request("p5_no_retry", LLMQuery(model="deepseek-chat", messages=[{"role": "user", "content": "fail once"}]))
        fail_status = fail_res["response"].status_code
    return {
        "task_survived_broken_log_pipe": ok,
        "dropped_logs_safely_absorbed": handler.dropped_logs,
        "uncertain_failure_status_code": fail_status,
        "provider_calls_on_uncertain_failure": failing.calls,
        "zero_duplicate_retries_verified": failing.calls == 1 and fail_status >= 400,
    }


def main() -> None:
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "priority_1_queue_and_3way_baseline": {
            "queue_wait": bench_queue_wait(),
            "three_way_baseline": bench_three_way_baseline(),
        },
        "priority_2_resident_task_scope": bench_p2_cold_vs_resident(),
        "priority_3_context_control": bench_p3_context_token_savings(),
        "priority_4_validation_and_parallel_tools": bench_p4_validation_and_parallel_tools(),
        "priority_5_recovery_and_log_decoupling": bench_p5_log_fault_injection(),
    }
    out = PROJECT_ROOT / "reports" / "p1_to_p5_acceptance.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
