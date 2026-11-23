"""
agiresearch/AIOS (COLM 2025, ~6.6k stars) Aligned Official Kernel & Multi-Agent Benchmark Suite.

Evaluates 5 core dimensions with zero external API cost:
  1. Concurrent Multi-Agent Scheduling: Sequential vs AIOS-FIFO vs AIOS-RR (Avg/P90 waiting time, turnaround, speedup)
  2. Four-Queue Syscall Subsystem Throughput (LLM / Memory / Storage / Tool)
  3. A-Mem Associative Recall & Upstream AIOS Memory Access-Control Isolation Matrix
  4. LSFS Prompt-to-Syscall / Rollback / Security & ContextStore Deduplication/Gzip Compression
  5. Multi-Agent DAG Concurrency Scaling (1 vs 4 vs 8 workers) & Fault Branch Isolation
"""
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from aios.terminal.intent_router import IntentRouter
from cerebrum.llm.apis import LLMQuery
from cerebrum.memory.apis import MemoryQuery
from cerebrum.storage.apis import StorageQuery
from cerebrum.tool.apis import ToolQuery
from runtime.launch import AIOSKernelRuntime


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * pct / 100.0) - 1))
    return ordered[idx]


def bench_scheduling_concurrency(concurrency_levels=(4, 8, 16)) -> Dict[str, Any]:
    """Dimension 1: COLM 2025 FIFO vs RR waiting/turnaround fairness & concurrency speedup."""
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for n_agents in concurrency_levels:
            level_row: Dict[str, Any] = {"agents": n_agents}
            # 1) Unmanaged sequential baseline (no concurrent dispatch)
            t_seq0 = time.perf_counter()
            seq_waits: List[float] = []
            seq_turns: List[float] = []
            with AIOSKernelRuntime(
                scheduler_policy="fifo",
                storage_root=str(Path(tmp) / f"seq_{n_agents}"),
                batch_interval=0.005,
                log_mode="silent",
            ) as kernel:
                for i in range(n_agents):
                    is_heavy = (i % 4 == 0)
                    q = LLMQuery(model="mock-aios-core", messages=[{"role": "user", "content": "alpha beta gamma delta epsilon zeta" if is_heavy else "ok"}])
                    if is_heavy:
                        setattr(q, "per_token_delay", 0.006)
                    t_sub = time.perf_counter()
                    res = kernel.execute_request(f"seq_agent_{i}", q)
                    assert res["response"].status_code == 200
                    seq_waits.append(sum(res["waiting_times"]))
                    seq_turns.append(time.perf_counter() - t_sub)
            seq_wall = time.perf_counter() - t_seq0
            level_row["sequential"] = {
                "wall_seconds": round(seq_wall, 4),
                "avg_turnaround_ms": round(statistics.mean(seq_turns) * 1000, 2),
                "p90_turnaround_ms": round(_percentile(seq_turns, 90) * 1000, 2),
            }

            # 2) Concurrent AIOS-FIFO vs AIOS-RR
            for policy in ("fifo", "rr"):
                waits: List[float] = []
                light_waits: List[float] = []
                turns: List[float] = []
                heavy_slices: List[int] = []
                t0 = time.perf_counter()
                with AIOSKernelRuntime(
                    scheduler_policy=policy,
                    storage_root=str(Path(tmp) / f"{policy}_{n_agents}"),
                    batch_interval=0.005,
                    time_slice=0.015,
                    log_mode="silent",
                ) as kernel:
                    def run_one(idx: int):
                        is_heavy = (idx % 4 == 0)
                        q = LLMQuery(model="mock-aios-core", messages=[{"role": "user", "content": "alpha beta gamma delta epsilon zeta" if is_heavy else "ok"}])
                        if is_heavy:
                            setattr(q, "per_token_delay", 0.006)
                        t_sub = time.perf_counter()
                        res = kernel.execute_request(f"{policy}_agent_{idx}", q)
                        t_done = time.perf_counter()
                        assert res["response"].status_code == 200
                        return is_heavy, res["waiting_times"][0], t_done - t_sub, len(res["start_times"])

                    with ThreadPoolExecutor(max_workers=n_agents) as pool:
                        futs = [pool.submit(run_one, i) for i in range(n_agents)]
                        for fut in futs:
                            is_heavy, first_wait, turn, slices = fut.result(timeout=15)
                            waits.append(first_wait)
                            turns.append(turn)
                            if is_heavy:
                                heavy_slices.append(slices)
                            else:
                                light_waits.append(first_wait)
                wall = time.perf_counter() - t0
                level_row[policy] = {
                    "wall_seconds": round(wall, 4),
                    "speedup_vs_seq": round(seq_wall / wall, 2) if wall > 0 else 0.0,
                    "avg_wait_ms": round(statistics.mean(waits) * 1000, 2),
                    "p90_wait_ms": round(_percentile(waits, 90) * 1000, 2),
                    "light_p90_wait_ms": round(_percentile(light_waits, 90) * 1000, 2),
                    "avg_turnaround_ms": round(statistics.mean(turns) * 1000, 2),
                    "p90_turnaround_ms": round(_percentile(turns, 90) * 1000, 2),
                    "avg_heavy_slices": round(statistics.mean(heavy_slices), 2) if heavy_slices else 1.0,
                    "completed": n_agents,
                }
            results.append(level_row)
    return {"levels": results}


def bench_four_queue_throughput(ops_per_queue: int = 40) -> Dict[str, Any]:
    """Dimension 2: Four-queue syscall dispatch throughput & latency."""
    latencies: Dict[str, List[float]] = {"llm": [], "memory": [], "storage": [], "tool": []}
    ok_counts: Dict[str, int] = {"llm": 0, "memory": 0, "storage": 0, "tool": 0}
    with tempfile.TemporaryDirectory() as tmp:
        with AIOSKernelRuntime(
            scheduler_policy="fifo",
            storage_root=str(Path(tmp) / "store"),
            batch_interval=0.002,
            log_mode="silent",
        ) as kernel:
            t0 = time.perf_counter()

            def call_llm(i: int):
                s = time.perf_counter()
                r = kernel.execute_request(f"t_llm_{i % 4}", LLMQuery(model="mock-aios-core", messages=[{"role": "user", "content": f"ping {i}"}]))
                return "llm", time.perf_counter() - s, r["response"].status_code == 200

            def call_mem(i: int):
                s = time.perf_counter()
                r = kernel.execute_request(
                    f"t_mem_{i % 4}",
                    MemoryQuery(operation_type="add_memory", user_id="bench", params={"content": f"fact {i} about scheduler"}),
                )
                return "memory", time.perf_counter() - s, r["response"].status_code == 200

            def call_sto(i: int):
                s = time.perf_counter()
                r = kernel.execute_request(
                    f"t_sto_{i % 4}",
                    StorageQuery(operation_type="write", params={"path": f"bench/f_{i}.txt", "content": f"payload-{i}"}),
                )
                return "storage", time.perf_counter() - s, r["response"].status_code == 200

            def call_tool(i: int):
                s = time.perf_counter()
                r = kernel.execute_request(
                    f"t_tool_{i % 4}",
                    ToolQuery(tool_calls=[{"name": "math.calculate", "parameters": {"expression": f"({i}+5)*2"}}]),
                )
                return "tool", time.perf_counter() - s, r["response"].status_code == 200

            with ThreadPoolExecutor(max_workers=16) as pool:
                futs = []
                for i in range(ops_per_queue):
                    futs.append(pool.submit(call_llm, i))
                    futs.append(pool.submit(call_mem, i))
                    futs.append(pool.submit(call_sto, i))
                    futs.append(pool.submit(call_tool, i))
                for fut in futs:
                    kind, dur, ok = fut.result(timeout=15)
                    latencies[kind].append(dur)
                    if ok:
                        ok_counts[kind] += 1
            total_wall = time.perf_counter() - t0

    total_ops = ops_per_queue * 4
    total_ok = sum(ok_counts.values())
    per_queue = {
        k: {
            "passed": ok_counts[k],
            "total": ops_per_queue,
            "avg_ms": round(statistics.mean(v) * 1000, 2),
            "p95_ms": round(_percentile(v, 95) * 1000, 2),
        }
        for k, v in latencies.items()
    }
    return {
        "total_syscalls": total_ops,
        "success_rate": round(total_ok / total_ops, 4),
        "wall_seconds": round(total_wall, 4),
        "throughput_syscalls_per_sec": round(total_ops / total_wall, 2),
        "per_queue": per_queue,
    }


def bench_amem_and_isolation_matrix() -> Dict[str, Any]:
    """Dimension 3: A-Mem Zettelkasten recall + Upstream AIOS access-control matrix."""
    with tempfile.TemporaryDirectory() as tmp:
        with AIOSKernelRuntime(storage_root=str(Path(tmp) / "store"), log_mode="silent") as kernel:
            # Populate 24 notes across 6 topics (4 notes per topic)
            topics = [
                ("cgroup", "Linux cgroup v2 cpu.max throttling and memory.max OOM kill isolation"),
                ("scheduler", "AIOS RR time-slicing preemption and DRF dominant resource fairness"),
                ("context", "SHA256 content-addressed context deduplication and gzip byte budget"),
                ("mailbox", "Zero-copy Mailbox IPC with request_id idempotency and ACL transfer"),
                ("lsfs", "LSFS semantic file rollback share links and prompt intent routing"),
                ("litecua", "LiteCUA virtual desktop accessibility tree click type hotkey snapshot"),
            ]
            for topic, desc in topics:
                for j in range(4):
                    res = kernel.execute_request(
                        "curator",
                        MemoryQuery(
                            operation_type="add_memory",
                            user_id="u_alpha",
                            params={"content": f"[{topic}-{j}] {desc} detail_{j}", "metadata": {"policy": "shared"}},
                        ),
                    )
                    assert res["response"].status_code == 200

            notes = kernel.memory_manager.list_notes(user_id="u_alpha")
            linked_notes = sum(1 for n in notes if n.get("links"))
            evolved_notes = sum(1 for n in notes if n.get("evolution_count", 0) > 0)

            recall_hits = 0
            for topic, _ in topics:
                res = kernel.execute_request(
                    "reader_agent",
                    MemoryQuery(operation_type="retrieve_memory", user_id="u_alpha", params={"query": topic, "top_k": 3}),
                )
                content = str(res["response"].response_message or "")
                if topic in content.lower():
                    recall_hits += 1

            # 4-case Access-Control Matrix (aligned with upstream agiresearch/AIOS)
            priv_id = kernel.execute_request(
                "alice",
                MemoryQuery(operation_type="add_memory", user_id="u1", params={"content": "TOP_SECRET_ALPHA_KEY"}),
            )["response"].memory_id
            shared_id = kernel.execute_request(
                "alice",
                MemoryQuery(
                    operation_type="add_memory",
                    user_id="u1",
                    params={"content": "SHARED_TEAM_DOC", "metadata": {"policy": "shared"}},
                ),
            )["response"].memory_id

            matrix_checks = {
                "same_user_same_agent_private_allow": kernel.execute_request(
                    "alice", MemoryQuery(operation_type="get_memory", user_id="u1", params={"id": priv_id})
                )["response"].status_code == 200,
                "same_user_other_agent_private_deny": kernel.execute_request(
                    "bob", MemoryQuery(operation_type="get_memory", user_id="u1", params={"id": priv_id}, agent_id="alice")
                )["response"].status_code == 403,
                "same_user_other_agent_shared_allow": kernel.execute_request(
                    "bob", MemoryQuery(operation_type="get_memory", user_id="u1", params={"id": shared_id}, agent_id="alice")
                )["response"].status_code == 200,
                "cross_user_same_agent_private_deny": kernel.execute_request(
                    "alice", MemoryQuery(operation_type="get_memory", user_id="u2", params={"id": priv_id})
                )["response"].status_code == 403,
                "cross_user_shared_deny": kernel.execute_request(
                    "bob", MemoryQuery(operation_type="get_memory", user_id="u2", params={"id": shared_id}, agent_id="alice")
                )["response"].status_code == 403,
            }
            passed_matrix = sum(1 for v in matrix_checks.values() if v)
            return {
                "notes_indexed": len(notes),
                "linked_note_ratio": round(linked_notes / max(1, len(notes)), 4),
                "evolved_notes": evolved_notes,
                "topic_recall_at_3": round(recall_hits / len(topics), 4),
                "isolation_matrix_passed": passed_matrix,
                "isolation_matrix_total": len(matrix_checks),
                "isolation_checks": matrix_checks,
            }


def bench_lsfs_and_context_store() -> Dict[str, Any]:
    """Dimension 4: LSFS intent routing, rollback, path traversal defense & ContextStore compression."""
    with tempfile.TemporaryDirectory() as tmp:
        with AIOSKernelRuntime(storage_root=str(Path(tmp) / "store"), log_mode="silent") as kernel:
            router = IntentRouter()
            r_write1 = router.execute_prompt(kernel, "write 'version_one_body' to report.md", agent_name="bench-lsfs")
            r_write2 = router.execute_prompt(kernel, "write 'version_two_body' to report.md", agent_name="bench-lsfs")
            r_rb = router.execute_prompt(kernel, "rollback report.md by 1 version", agent_name="bench-lsfs")
            r_read = kernel.execute_request("bench-lsfs", StorageQuery(operation_type="read", params={"file_path": "report.md"}))
            rollback_exact = "version_one_body" in str(r_read["response"].response_message or "")

            traversal_blocked = (
                kernel.execute_request(
                    "bench-lsfs", StorageQuery(operation_type="read", params={"path": "../../etc/passwd"})
                )["response"].status_code
                >= 400
            )

            # ContextStore dedup & gzip compression
            blob = {"goal": "proj3_bench", "constraints": ["no-leak", "bounded"], "logs": "SYSCALL_TRACE_ENTRY_0123456789 " * 120}
            refs = [kernel.context_store.put("agent_a", blob) for _ in range(5)]
            dedup_exact = len(set(refs)) == 1
            c_stats = kernel.context_store.stats()

            return {
                "intent_prompts_succeeded": all(r.get("status_code", 200) == 200 for r in (r_write1, r_write2, r_rb)),
                "rollback_exact": rollback_exact,
                "path_traversal_blocked": traversal_blocked,
                "context_dedup_exact": dedup_exact,
                "context_dedup_hits": c_stats["dedup_hits"],
                "context_storage_saving_ratio": c_stats["storage_saving_ratio"],
            }


def bench_dag_scaling_and_fault_isolation() -> Dict[str, Any]:
    """Dimension 5: Multi-Agent DAG scaling (1 vs 4 vs 8 workers) & fault branch isolation."""
    with tempfile.TemporaryDirectory() as tmp:
        with AIOSKernelRuntime(storage_root=str(Path(tmp) / "store"), log_mode="silent") as kernel:
            def slow_ok(task, upstream, attempt):
                time.sleep(0.05)
                return {"v": 1}

            tasks = [{"id": f"b{i}", "handler": slow_ok} for i in range(8)]
            tasks.append({"id": "join", "depends_on": [f"b{i}" for i in range(8)], "handler": lambda t, u, a: {"sum": len(u)}})

            timings: Dict[str, float] = {}
            for w in (1, 4, 8):
                t0 = time.perf_counter()
                out = kernel.workflow_engine.run_workflow(f"scale_{w}", tasks, max_workers=w)
                timings[f"w{w}_sec"] = round(time.perf_counter() - t0, 4)
                assert out["results"]["join"] == {"sum": 8}

            # Fault injection: 2 failing branches + 6 healthy branches
            def fail_branch(*_):
                raise RuntimeError("injected branch failure")

            fault_tasks = []
            for i in range(8):
                if i < 2:
                    fault_tasks.append({"id": f"f{i}", "handler": fail_branch, "max_retries": 0})
                    fault_tasks.append({"id": f"f{i}_down", "depends_on": [f"f{i}"], "handler": slow_ok})
                else:
                    fault_tasks.append({"id": f"h{i}", "handler": slow_ok})
            fault_out = kernel.workflow_engine.run_workflow("fault_iso", fault_tasks, max_workers=8)
            healthy_survived = sum(1 for i in range(2, 8) if fault_out["states"][f"h{i}"] == "succeeded")
            isolated_down = sum(1 for i in range(2) if fault_out["states"][f"f{i}_down"] == "isolated_skipped")

            return {
                **timings,
                "speedup_4w": round(timings["w1_sec"] / timings["w4_sec"], 2),
                "speedup_8w": round(timings["w1_sec"] / timings["w8_sec"], 2),
                "healthy_branch_survival": f"{healthy_survived}/6",
                "fault_downstream_isolated": f"{isolated_down}/2",
                "drf_leaked_cpu_milli": kernel.scheduler.drf_controller.used.cpu_milli,
            }


def main() -> None:
    report = {
        "benchmark": "agiresearch/AIOS (COLM 2025, ~6.6k stars) Aligned Official Suite",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dimensions": {
            "1_scheduling_concurrency": bench_scheduling_concurrency(),
            "2_four_queue_throughput": bench_four_queue_throughput(),
            "3_amem_and_isolation_matrix": bench_amem_and_isolation_matrix(),
            "4_lsfs_and_context_store": bench_lsfs_and_context_store(),
            "5_dag_scaling_and_fault_isolation": bench_dag_scaling_and_fault_isolation(),
        },
    }

    # Compute overall scorecard (100-point rubric across the 5 dimensions)
    d1 = report["dimensions"]["1_scheduling_concurrency"]["levels"][-1]
    d2 = report["dimensions"]["2_four_queue_throughput"]
    d3 = report["dimensions"]["3_amem_and_isolation_matrix"]
    d4 = report["dimensions"]["4_lsfs_and_context_store"]
    d5 = report["dimensions"]["5_dag_scaling_and_fault_isolation"]

    s1 = 20.0 if (d1["fifo"]["speedup_vs_seq"] >= 2.0 and d1["rr"]["avg_heavy_slices"] > 1.0) else 15.0
    s2 = round(20.0 * d2["success_rate"], 2)
    s3 = round(10.0 * d3["topic_recall_at_3"] + 10.0 * (d3["isolation_matrix_passed"] / d3["isolation_matrix_total"]), 2)
    s4 = round(
        (5.0 if d4["intent_prompts_succeeded"] else 0.0)
        + (5.0 if d4["rollback_exact"] else 0.0)
        + (5.0 if d4["path_traversal_blocked"] else 0.0)
        + (5.0 if (d4["context_dedup_exact"] and d4["context_storage_saving_ratio"] >= 0.8) else 0.0),
        2,
    )
    s5 = round(
        (10.0 if d5["speedup_8w"] >= 4.0 else 6.0)
        + (10.0 if (d5["healthy_branch_survival"] == "6/6" and d5["fault_downstream_isolated"] == "2/2" and d5["drf_leaked_cpu_milli"] == 0) else 0.0),
        2,
    )
    report["scorecard"] = {
        "scheduling_concurrency": s1,
        "four_queue_throughput": s2,
        "amem_and_isolation": s3,
        "lsfs_and_context": s4,
        "dag_and_fault_isolation": s5,
        "total_score": round(s1 + s2 + s3 + s4 + s5, 2),
        "max_score": 100.0,
    }

    out_path = PROJECT_ROOT / "reports" / "aios_official_benchmark.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
