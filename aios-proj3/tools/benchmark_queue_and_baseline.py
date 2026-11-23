"""
Priority 1 Verification & Independent 3-Way Baseline Benchmark.

1. Measures exact per-call LLM queue waiting latency & throughput under fixed input/output/tokens:
   - Local/mock batched path (batch_interval=0.02)
   - Cloud API path (simulated fixed-latency cloud ModelService, zero real API cost)
2. Independent 3-Way Baseline (no idle kernel in bare direct group):
   - Mode A: Bare Direct Call (pure function/handler, no AIOS kernel, zero idle threads)
   - Mode B: Resident AIOS Kernel (single-agent sequential calls, reusing kernel)
   - Mode C: Resident AIOS Kernel (8-agent concurrent calls, reusing kernel)
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
import tracemalloc
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from cerebrum.llm.apis import LLMQuery, LLMResponse
from runtime.launch import AIOSKernelRuntime


class FixedCloudStubService:
    """Deterministic stub mimicking a cloud API (fixed 5ms latency, fixed token count, zero batching benefit)."""

    def __init__(self, latency_sec: float = 0.005):
        self.latency_sec = latency_sec

    def complete(self, query: LLMQuery) -> LLMResponse:
        time.sleep(self.latency_sec)
        return LLMResponse(
            response_message="fixed_cloud_reply_ok",
            finished=True,
            status_code=200,
            usage={"prompt_tokens": 120, "completion_tokens": 16, "total_tokens": 136},
        )


def bench_queue_wait(calls: int = 20, batch_interval: float = 0.02) -> Dict[str, Any]:
    results: Dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        # 1) Local/mock backend with batch_interval=20ms
        waits_local: List[float] = []
        t0 = time.perf_counter()
        with AIOSKernelRuntime(
            scheduler_policy="fifo",
            storage_root=str(Path(tmp) / "local"),
            batch_interval=batch_interval,
            log_mode="silent",
        ) as kernel:
            for i in range(calls):
                r = kernel.execute_request(
                    "agent_local",
                    LLMQuery(model="mock-aios-core", messages=[{"role": "user", "content": f"fixed prompt {i}"}]),
                )
                assert r["response"].status_code == 200
                waits_local.append(r["waiting_times"][0] * 1000.0)
        wall_local = time.perf_counter() - t0
        results["local_batch_20ms"] = {
            "calls": calls,
            "avg_wait_ms": round(statistics.mean(waits_local), 3),
            "median_wait_ms": round(statistics.median(waits_local), 3),
            "max_wait_ms": round(max(waits_local), 3),
            "wall_seconds": round(wall_local, 4),
            "throughput_calls_per_sec": round(calls / wall_local, 2),
        }

        # 2) Cloud API backend (via model_service) with batch_interval=20ms
        waits_cloud: List[float] = []
        t1 = time.perf_counter()
        with AIOSKernelRuntime(
            scheduler_policy="fifo",
            storage_root=str(Path(tmp) / "cloud"),
            batch_interval=batch_interval,
            log_mode="silent",
        ) as kernel:
            kernel.llm.model_service = FixedCloudStubService(latency_sec=0.005)
            for i in range(calls):
                r = kernel.execute_request(
                    "agent_cloud",
                    LLMQuery(model="deepseek-chat", messages=[{"role": "user", "content": f"fixed prompt {i}"}]),
                )
                assert r["response"].status_code == 200
                waits_cloud.append(r["waiting_times"][0] * 1000.0)
        wall_cloud = time.perf_counter() - t1
        results["cloud_api_path"] = {
            "calls": calls,
            "avg_wait_ms": round(statistics.mean(waits_cloud), 3),
            "median_wait_ms": round(statistics.median(waits_cloud), 3),
            "max_wait_ms": round(max(waits_cloud), 3),
            "wall_seconds": round(wall_cloud, 4),
            "throughput_calls_per_sec": round(calls / wall_cloud, 2),
        }
    return results


def bench_three_way_baseline(total_calls: int = 24, simulated_io_sec: float = 0.01) -> Dict[str, Any]:
    """Independent 3-way baseline: (1) Bare Direct Call (no kernel), (2) Resident Kernel Sequential, (3) Resident Kernel Concurrent."""
    stub = FixedCloudStubService(latency_sec=simulated_io_sec)

    # Mode A1: Bare Direct Call Sequential (zero kernel instantiated)
    tracemalloc.start()
    t0 = time.perf_counter()
    for i in range(total_calls):
        resp = stub.complete(LLMQuery(model="deepseek-chat", messages=[{"role": "user", "content": f"q {i}"}]))
        assert resp.status_code == 200
    wall_bare_seq = time.perf_counter() - t0
    _, peak_bare_seq = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # Mode A2: Bare Direct Call Concurrent 8 (fair apples-to-apples concurrent baseline)
    tracemalloc.start()
    t0c = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [
            pool.submit(stub.complete, LLMQuery(model="deepseek-chat", messages=[{"role": "user", "content": f"q {i}"}]))
            for i in range(total_calls)
        ]
        for fut in futs:
            assert fut.result(timeout=10).status_code == 200
    wall_bare_conc = time.perf_counter() - t0c
    _, peak_bare_conc = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    with tempfile.TemporaryDirectory() as tmp:
        # Mode B: Resident Kernel Sequential
        tracemalloc.start()
        with AIOSKernelRuntime(
            scheduler_policy="fifo",
            storage_root=str(Path(tmp) / "seq"),
            batch_interval=0.02,
            log_mode="silent",
        ) as kernel:
            kernel.llm.model_service = stub
            t1 = time.perf_counter()
            for i in range(total_calls):
                r = kernel.execute_request(
                    f"seq_{i % 4}",
                    LLMQuery(model="deepseek-chat", messages=[{"role": "user", "content": f"q {i}"}]),
                )
                assert r["response"].status_code == 200
            wall_res_seq = time.perf_counter() - t1
        _, peak_res_seq = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Mode C: Resident Kernel Concurrent (8 agents via DAG / ThreadPool)
        tracemalloc.start()
        with AIOSKernelRuntime(
            scheduler_policy="fifo",
            storage_root=str(Path(tmp) / "conc"),
            batch_interval=0.02,
            log_mode="silent",
        ) as kernel:
            kernel.llm.model_service = stub
            t2 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = [
                    pool.submit(
                        kernel.execute_request,
                        f"agent_{i % 8}",
                        LLMQuery(model="deepseek-chat", messages=[{"role": "user", "content": f"q {i}"}]),
                    )
                    for i in range(total_calls)
                ]
                for fut in futs:
                    assert fut.result(timeout=10)["response"].status_code == 200
            wall_res_conc = time.perf_counter() - t2
        _, peak_res_conc = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    return {
        "total_calls": total_calls,
        "simulated_provider_io_ms": simulated_io_sec * 1000.0,
        "bare_direct_sequential": {
            "wall_seconds": round(wall_bare_seq, 4),
            "throughput_calls_per_sec": round(total_calls / wall_bare_seq, 2),
            "peak_py_memory_kib": round(peak_bare_seq / 1024.0, 1),
        },
        "bare_direct_concurrent_8": {
            "wall_seconds": round(wall_bare_conc, 4),
            "throughput_calls_per_sec": round(total_calls / wall_bare_conc, 2),
            "peak_py_memory_kib": round(peak_bare_conc / 1024.0, 1),
        },
        "resident_kernel_sequential": {
            "wall_seconds": round(wall_res_seq, 4),
            "overhead_vs_bare_seq_percent": round(100.0 * (wall_res_seq - wall_bare_seq) / wall_bare_seq, 2),
            "throughput_calls_per_sec": round(total_calls / wall_res_seq, 2),
            "peak_py_memory_kib": round(peak_res_seq / 1024.0, 1),
        },
        "resident_kernel_concurrent_8": {
            "wall_seconds": round(wall_res_conc, 4),
            "overhead_vs_bare_conc_8_percent": round(100.0 * (wall_res_conc - wall_bare_conc) / wall_bare_conc, 2),
            "speedup_vs_bare_seq": round(wall_bare_seq / wall_res_conc, 2),
            "throughput_calls_per_sec": round(total_calls / wall_res_conc, 2),
            "peak_py_memory_kib": round(peak_res_conc / 1024.0, 1),
        },
    }


def main() -> None:
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "queue_wait_benchmark": bench_queue_wait(),
        "three_way_baseline": bench_three_way_baseline(),
    }
    out = PROJECT_ROOT / "reports" / "queue_opt_and_baseline.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
