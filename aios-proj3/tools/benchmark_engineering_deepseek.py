"""
Real-Model 3-Project Controlled Python Engineering Repair Benchmark on AIOS Formal Kernel.

Evaluates all 3 controlled engineering projects (`pricing`, `intervals`, `text_normalization`)
through `NativeConsole.conversations.submit(..., mode='engineering')`, running all 7 kernel
workflow phases:
  inspect -> baseline -> plan -> patch (real LLM) -> verify (isolated unittest) -> review -> answer/deliver (LSFS readback SHA256).

API keys are read strictly from `DEEPSEEK_API_KEY` in process memory and never written to disk.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Dict, List
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole

PROJECTS = ("pricing", "intervals", "text_normalization")


def run_benchmark(model: str = "deepseek-chat", base_url: str = "https://api.deepseek.com/v1") -> Dict[str, Any]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY environment variable is required")

    rows: List[Dict[str, Any]] = []
    t_all = time.perf_counter()

    with tempfile.TemporaryDirectory() as folder:
        with AIOSKernelRuntime(storage_root=str(Path(folder) / "store"), log_mode="silent") as kernel:
            console = NativeConsole(kernel, Path(folder) / "jobs")
            try:
                console.model_service.request(
                    "save",
                    {
                        "id": "deepseek_eng",
                        "label": "DeepSeek Engineering",
                        "provider": "deepseek",
                        "base_url": base_url,
                        "model": model,
                        "key": api_key,
                    },
                )
                console.model_service.request("select", {"id": "deepseek_eng"})

                for project in PROJECTS:
                    t0 = time.perf_counter()
                    res = console.conversations.submit(
                        {
                            "conversation_id": f"eng_{project}_{uuid4().hex[:8]}",
                            "request_id": uuid4().hex,
                            "mode": "engineering",
                            "project": project,
                            "prompt": f"修复受控工程项目 {project} 并通过全部隔离单元测试",
                        }
                    )
                    elapsed = round(time.perf_counter() - t0, 2)
                    passed = res.get("status") == "succeeded"
                    verified = False
                    artifact_readback = False
                    tests_run = None
                    if passed:
                        try:
                            payload = json.loads(res.get("answer", "{}"))
                            verified = bool(payload.get("success"))
                            artifact_readback = bool((payload.get("artifact") or {}).get("readback_verified"))
                            tests_run = payload.get("tests_run")
                        except Exception:
                            pass
                    row = {
                        "project": project,
                        "passed": bool(passed and verified and artifact_readback),
                        "status": res.get("status"),
                        "verified": verified,
                        "artifact_readback_verified": artifact_readback,
                        "tests_run": tests_run,
                        "seconds": elapsed,
                        "budget_used": res.get("budget_used"),
                    }
                    rows.append(row)
                    print(
                        f"[{project}] {'PASS' if row['passed'] else 'FAIL'} "
                        f"verified={verified} readback={artifact_readback} time={elapsed}s",
                        flush=True,
                    )
                total_syscalls = kernel.syscall_executor.id
            finally:
                console.close()

    passed_count = sum(1 for r in rows if r["passed"])
    return {
        "benchmark": "AIOS Controlled 3-Project Engineering Repair (7-Stage Isolated Verification)",
        "runtime": "AIOS Formal Kernel + NativeConsole + Isolated Python Worker",
        "model": model,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "projects_evaluated": len(rows),
            "projects_passed": passed_count,
            "pass_rate": round(passed_count / max(1, len(rows)), 4),
            "score_percent": round(100.0 * passed_count / max(1, len(rows)), 2),
            "total_kernel_syscalls": total_syscalls,
            "wall_seconds": round(time.perf_counter() - t_all, 2),
        },
        "projects": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reports" / "engineering_repair_deepseek.json")
    args = parser.parse_args()

    report = run_benchmark(model=args.model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("ENGINEERING_BENCH_SUMMARY " + json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
