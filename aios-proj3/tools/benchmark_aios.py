"""Reproducible AIOS Kernel scheduling, RR time-slicing & ContextStore compression benchmark."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from cerebrum.llm.apis import LLMQuery
from runtime.launch import AIOSKernelRuntime


def run_benchmark(repeats: int = 3) -> dict:
    samples = []
    with tempfile.TemporaryDirectory() as tmp:
        for rep in range(repeats):
            for policy in (("fifo", "rr") if rep % 2 == 0 else ("rr", "fifo")):
                storage_dir = str(Path(tmp) / f"run_{rep}_{policy}")
                t0 = time.monotonic()
                with AIOSKernelRuntime(
                    scheduler_policy=policy,
                    storage_root=storage_dir,
                    batch_interval=0.01,
                    time_slice=0.02,
                    log_mode="silent",
                ) as kernel:
                    # Submit one multi-token query + one fast query
                    q_long = LLMQuery(messages=[{"role": "user", "content": "alpha beta gamma delta epsilon"}])
                    setattr(q_long, "per_token_delay", 0.008)
                    q_short = LLMQuery(messages=[{"role": "user", "content": "fast"}])

                    r_long = kernel.execute_request("heavy_agent", q_long)
                    r_short = kernel.execute_request("light_agent", q_short)

                    # Populate ContextStore for compression ratio verification
                    payload = {"goal": "benchmark", "trace": "LINE_DATA_0123456789 " * 80}
                    kernel.context_store.put("heavy_agent", payload)
                    kernel.context_store.put("heavy_agent", payload)
                    c_stats = kernel.context_store.stats()

                elapsed = time.monotonic() - t0
                samples.append(
                    {
                        "repeat": rep,
                        "policy": policy,
                        "wall_seconds": round(elapsed, 6),
                        "long_query_slices": len(r_long["start_times"]),
                        "short_query_wait_sec": round(r_short["waiting_times"][0], 6),
                        "context_saving_ratio": c_stats["storage_saving_ratio"],
                    }
                )

    medians = {
        pol: {
            "wall_seconds": round(statistics.median(s["wall_seconds"] for s in samples if s["policy"] == pol), 6),
            "long_query_slices": statistics.median(s["long_query_slices"] for s in samples if s["policy"] == pol),
            "context_saving_ratio": statistics.median(
                s["context_saving_ratio"] for s in samples if s["policy"] == pol
            ),
        }
        for pol in ("fifo", "rr")
    }
    return {"scenario": "AIOS FIFO vs RR Time-Slicing & Context Deduplication/Gzip", "medians": medians, "samples": samples}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reports" / "benchmark.json")
    args = parser.parse_args()

    result = run_benchmark(repeats=args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result["medians"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
