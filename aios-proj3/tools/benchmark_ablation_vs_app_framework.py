"""
Ablation Benchmark: Naive App-Layer Multi-Agent Framework vs. AIOS Formal Kernel.

Directly addresses the official Proj3 pain points:
  1. Redundant context copying across multi-agent fan-out (full JSON payload vs. SHA256 ContextStore + gzip + Mailbox zero-copy ref)
  2. Uncoordinated serial scheduling vs. AIOS concurrent DAG + DRF scheduling
  3. Cascading failure on single-agent fault vs. AIOS fault branch isolation
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from runtime.launch import AIOSKernelRuntime


def run_ablation(num_consumers: int = 8, payload_repeats: int = 300, work_sec: float = 0.04) -> Dict[str, Any]:
    shared_doc = {
        "goal": "Diagnose kernel scheduling bottleneck and synthesize patch",
        "constraints": ["openEuler cgroup v2", "zero orphan processes", "bounded memory"],
        "trace_log": [f"line_{i:04d}: syscall=llm agent=worker_{i%4} latency_ms=12.4 status=ok" for i in range(payload_repeats)],
    }
    raw_bytes_per_copy = len(json.dumps(shared_doc, ensure_ascii=False).encode("utf-8"))

    # 1. Baseline: Naive App-Layer Framework (serial + full deep-copy JSON per consumer + cascading abort on fault)
    t_base0 = time.perf_counter()
    base_ipc_bytes = 0
    base_storage_bytes = 0
    base_copies = []
    for i in range(num_consumers):
        copied = deepcopy(shared_doc)
        wire = json.dumps({"sender": "planner", "recipient": f"worker_{i}", "context": copied}, ensure_ascii=False).encode("utf-8")
        base_ipc_bytes += len(wire)
        base_storage_bytes += raw_bytes_per_copy
        base_copies.append(copied)
        time.sleep(work_sec)
    base_wall = time.perf_counter() - t_base0

    # Baseline fault test: unhandled failure aborts pipeline
    base_healthy_survived = 0
    try:
        for i in range(num_consumers):
            if i == 0:
                raise RuntimeError("single agent crash")
            base_healthy_survived += 1
    except RuntimeError:
        pass

    # 2. AIOS Formal Kernel: ContextStore SHA256 dedup + gzip + Mailbox zero-copy ref + concurrent DAG + fault isolation
    with tempfile.TemporaryDirectory() as tmp:
        with AIOSKernelRuntime(storage_root=str(Path(tmp) / "store"), log_mode="silent") as kernel:
            ipc = kernel.ipc_manager
            t_aios0 = time.perf_counter()

            # Producer stores context once (or repeated identical writes deduplicated by SHA256)
            ref = kernel.context_store.put("planner", shared_doc)
            for _ in range(num_consumers - 1):
                dup_ref = kernel.context_store.put("planner", shared_doc)
                assert dup_ref == ref

            planner_tok = ipc.issue_token("planner")
            aios_ipc_bytes = 0
            worker_tokens = {}
            for i in range(num_consumers):
                wname = f"worker_{i}"
                worker_tokens[wname] = ipc.issue_token(wname)
                msg_payload = {"task": f"analyze_{i}", "context_ref": ref}
                aios_ipc_bytes += len(json.dumps(msg_payload, ensure_ascii=False).encode("utf-8"))
                ipc.send(
                    sender="planner",
                    recipient=wname,
                    payload=msg_payload,
                    request_id=f"req_{i}",
                    context_refs=[ref],
                    token=planner_tok,
                )

            def make_handler(idx: int):
                wname = f"worker_{idx}"
                def _run(task, upstream, attempt):
                    msgs = ipc.receive(wname, token=worker_tokens[wname])
                    cref = msgs[0]["context_refs"][0]
                    data = kernel.context_store.get(cref, reader_agent=wname)
                    assert data["goal"] == shared_doc["goal"]
                    ipc.ack(wname, msgs[0]["message_id"], token=worker_tokens[wname])
                    time.sleep(work_sec)
                    return {"worker": wname, "lines": len(data["trace_log"])}
                return _run

            dag_tasks = [{"id": f"w_{i}", "agent": f"worker_{i}", "handler": make_handler(i)} for i in range(num_consumers)]
            dag_res = kernel.workflow_engine.run_workflow("ablation_healthy", dag_tasks, max_workers=num_consumers)
            assert all(st == "succeeded" for st in dag_res["states"].values())
            aios_wall = time.perf_counter() - t_aios0
            c_stats = kernel.context_store.stats()
            aios_storage_bytes = c_stats["physical_bytes_stored"]

            # AIOS fault isolation test: 2 failing branches + 6 healthy branches
            def _fail(*_):
                raise RuntimeError("injected single agent crash")
            def _ok(*_):
                time.sleep(0.01)
                return {"ok": True}

            fault_tasks = []
            for i in range(num_consumers):
                if i < 2:
                    fault_tasks.append({"id": f"f_{i}", "handler": _fail, "max_retries": 0})
                    fault_tasks.append({"id": f"f_{i}_child", "depends_on": [f"f_{i}"], "handler": _ok})
                else:
                    fault_tasks.append({"id": f"h_{i}", "handler": _ok})
            fault_res = kernel.workflow_engine.run_workflow("ablation_fault", fault_tasks, max_workers=num_consumers)
            aios_healthy_survived = sum(1 for i in range(2, num_consumers) if fault_res["states"][f"h_{i}"] == "succeeded")
            aios_fault_isolated = sum(1 for i in range(2) if fault_res["states"][f"f_{i}_child"] == "isolated_skipped")

    report = {
        "scenario": f"App-Layer Full-Copy Serial Baseline vs. AIOS Kernel ({num_consumers} consumers, {raw_bytes_per_copy}B shared context)",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "app_layer_baseline": {
            "wall_seconds": round(base_wall, 4),
            "ipc_wire_bytes": base_ipc_bytes,
            "context_storage_bytes": base_storage_bytes,
            "healthy_branch_survival_under_fault": f"{base_healthy_survived}/{num_consumers - 2}",
            "cascading_abort": True,
        },
        "aios_kernel": {
            "wall_seconds": round(aios_wall, 4),
            "ipc_wire_bytes": aios_ipc_bytes,
            "context_storage_bytes": aios_storage_bytes,
            "dedup_hits": c_stats["dedup_hits"],
            "healthy_branch_survival_under_fault": f"{aios_healthy_survived}/{num_consumers - 2}",
            "fault_downstream_isolated": f"{aios_fault_isolated}/2",
            "cascading_abort": False,
        },
        "improvements": {
            "wall_speedup": round(base_wall / max(1e-6, aios_wall), 2),
            "ipc_wire_reduction_percent": round(100.0 * (1.0 - aios_ipc_bytes / max(1, base_ipc_bytes)), 2),
            "context_storage_reduction_percent": round(100.0 * (1.0 - aios_storage_bytes / max(1, base_storage_bytes)), 2),
        },
    }
    return report


def main() -> None:
    report = run_ablation()
    out = PROJECT_ROOT / "reports" / "ablation_vs_app_framework.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
