"""
Real ModelService Concurrency Benchmark (Separating Simulated Stub vs Real RAM-Only Key Process Pipeline).

Compares 3 real-API modes using tiny 8-token prompts on `deepseek-chat`:
  1. Bare Direct HTTPS (4 concurrent threads, no AIOS kernel, no ModelService)
  2. AIOS Kernel + real ModelService (4 sequential calls)
  3. AIOS Kernel + real ModelService (4 concurrent calls — explicitly measuring single-pipe lock serialization)

API key is read strictly from `DEEPSEEK_API_KEY` in process memory and never written to disk.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from aios.llm_core.model_service import ModelService
from cerebrum.llm.apis import LLMQuery
from runtime.launch import AIOSKernelRuntime


def _bare_https_call(api_key: str, model: str, idx: int) -> float:
    t0 = time.perf_counter()
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": f"Reply with just the number {idx}."}],
        "max_tokens": 8,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        assert data.get("choices"), "missing choices"
    return time.perf_counter() - t0


def run(n_calls: int = 4, model: str = "deepseek-chat") -> dict:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY required")

    # 1. Bare Direct HTTPS (4 concurrent)
    t_b0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n_calls) as pool:
        bare_lat = list(pool.map(lambda i: _bare_https_call(api_key, model, i), range(n_calls)))
    bare_conc_wall = time.perf_counter() - t_b0

    # 2 & 3. AIOS Kernel + real ModelService (isolated RAM-only key process)
    ms = ModelService()
    ms.request("save", {
        "id": "deepseek_real",
        "label": "DeepSeek Real",
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model": model,
        "key": api_key,
    })
    ms.request("select", {"id": "deepseek_real"})

    try:
        with tempfile.TemporaryDirectory() as tmp:
            with AIOSKernelRuntime(storage_root=str(Path(tmp) / "store"), log_mode="silent") as kernel:
                kernel.llm.model_service = ms

                # 2. Sequential via AIOS Kernel + ModelService
                t_s0 = time.perf_counter()
                seq_waits = []
                for i in range(n_calls):
                    r = kernel.execute_request(
                        f"real_seq_{i}",
                        LLMQuery(model=model, messages=[{"role": "user", "content": f"Reply with just the number {i}."}], max_new_tokens=8, temperature=0.0),
                    )
                    assert r["response"].status_code == 200
                    seq_waits.append(r["waiting_times"][0] * 1000.0)
                ms_seq_wall = time.perf_counter() - t_s0

                # 3. Concurrent (4) via AIOS Kernel + ModelService
                t_c0 = time.perf_counter()
                def _one(i: int):
                    r = kernel.execute_request(
                        f"real_conc_{i}",
                        LLMQuery(model=model, messages=[{"role": "user", "content": f"Reply with just the number {i}."}], max_new_tokens=8, temperature=0.0),
                    )
                    assert r["response"].status_code == 200
                    return r["waiting_times"][0] * 1000.0
                with ThreadPoolExecutor(max_workers=n_calls) as pool:
                    conc_waits = list(pool.map(_one, range(n_calls)))
                ms_conc_wall = time.perf_counter() - t_c0
    finally:
        ms.close()

    return {
        "benchmark": "Real Cloud API (deepseek-chat) Concurrency: Bare HTTPS vs AIOS Kernel + RAM-Only ModelService",
        "calls_per_mode": n_calls,
        "model": model,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bare_https_concurrent_4": {
            "wall_seconds": round(bare_conc_wall, 3),
            "avg_call_seconds": round(sum(bare_lat) / len(bare_lat), 3),
            "throughput_calls_per_sec": round(n_calls / bare_conc_wall, 2),
        },
        "aios_kernel_model_service_sequential_4": {
            "wall_seconds": round(ms_seq_wall, 3),
            "avg_queue_wait_ms": round(sum(seq_waits) / len(seq_waits), 3),
            "throughput_calls_per_sec": round(n_calls / ms_seq_wall, 2),
        },
        "aios_kernel_model_service_concurrent_4": {
            "wall_seconds": round(ms_conc_wall, 3),
            "avg_queue_wait_ms": round(sum(conc_waits) / len(conc_waits), 3),
            "throughput_calls_per_sec": round(n_calls / ms_conc_wall, 2),
            "note": "ModelService uses request-ID multiplexed IPC with an internal ThreadPoolExecutor inside the isolated key-holding process, enabling true multi-agent concurrency without leaking supplier keys.",
        },
    }


def main() -> None:
    report = run()
    out = PROJECT_ROOT / "reports" / "real_model_service_concurrency_nuc.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
