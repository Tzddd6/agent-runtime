"""
Real-Model (deepseek-chat) End-to-End Verification of:
  1. Concurrent Multiplexed ModelService (4 concurrent real API calls vs Bare 4-concurrent HTTPS)
  2. Resident TaskScope + Context Compaction + Independent Linux VmRSS/VmHWM Peak RSS Measurement
     on real tau2-bench Airline tasks (comparing Cold/Uncompacted vs Resident/Compacted).

API key is read strictly from `DEEPSEEK_API_KEY` in process memory and never written to disk.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from aios.llm_core.model_service import ModelService
from cerebrum.interface import AutoTool
from cerebrum.llm.apis import LLMQuery
from cerebrum.tool.apis import ToolQuery
from runtime.launch import AIOSKernelRuntime
from tools.benchmark_real_model_service import run as run_concurrent_model_service
from tools.benchmark_tau2_airline import AirlineToolKit, TOOL_SCHEMAS, _normalize


def _read_proc_mem_kib() -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {"vm_rss_kib": None, "vm_hwm_kib": None}
    if sys.platform != "linux":
        return out
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                out["vm_rss_kib"] = int(line.split()[1])
            elif line.startswith("VmHWM:"):
                out["vm_hwm_kib"] = int(line.split()[1])
    except (OSError, ValueError):
        pass
    return out


def _compact_tau2_messages(msgs: List[Dict[str, Any]], keep_recent_tools: int = 2) -> List[Dict[str, Any]]:
    """Preserve system policy, all user turns, and recent tool outputs; compact older large tool JSONs."""
    tool_idxs = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    old_idxs = set(tool_idxs[:-keep_recent_tools]) if len(tool_idxs) > keep_recent_tools else set()
    if not old_idxs:
        return list(msgs)
    out = []
    for i, m in enumerate(msgs):
        if i in old_idxs and len(str(m.get("content", ""))) > 600:
            raw = str(m["content"])
            out.append({**m, "content": raw[:240] + f"...[compacted {len(raw)} chars]"})
        else:
            out.append(m)
    return out


def _run_tau2_subset(
    data_dir: Path,
    api_key: str,
    model: str,
    limit: int,
    resident_and_compacted: bool,
) -> Dict[str, Any]:
    base_db = json.loads((data_dir / "db.json").read_text(encoding="utf-8"))
    policy = (data_dir / "policy.md").read_text(encoding="utf-8")
    tasks = json.loads((data_dir / "tasks.json").read_text(encoding="utf-8"))[:limit]

    active_kit: Dict[str, Optional[AirlineToolKit]] = {"kit": None}
    for schema in TOOL_SCHEMAS:
        tname = schema["function"]["name"]
        def _mk(name):
            return lambda params: active_kit["kit"].dispatch(name, params)
        AutoTool.register(f"tau2/{tname}", _mk(tname), schema["function"]["description"])

    ms = ModelService()
    ms.request("save", {
        "id": "deepseek_p2p3", "label": "DeepSeek P2P3", "provider": "deepseek",
        "base_url": "https://api.deepseek.com/v1", "model": model, "key": api_key,
    })
    ms.request("select", {"id": "deepseek_p2p3"})

    t_all = time.perf_counter()
    rss_before = _read_proc_mem_kib()
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_tokens = 0
    passed = 0
    total_calls = 0

    def run_one_task(kernel: AIOSKernelRuntime, idx: int, task: Dict[str, Any]) -> bool:
        nonlocal total_prompt_tokens, total_completion_tokens, total_tokens, total_calls
        kit = AirlineToolKit(deepcopy(base_db))
        active_kit["kit"] = kit
        instr = task["user_scenario"]["instructions"]
        user_sys = (
            "You are simulating a customer talking to an airline customer service agent.\n"
            f"Known info:\n{instr.get('known_info') or ''}\n\n"
            f"Reason for call:\n{instr.get('reason_for_call') or ''}\n\n"
            f"Task instructions:\n{instr.get('task_instructions') or ''}\n\n"
            "When resolved or definitively refused/transferred, end with ###STOP###."
        )
        agent_sys = f"You are a helpful airline customer service agent. Strictly follow all policies:\n\n{policy}"
        user_msgs = [{"role": "system", "content": user_sys}, {"role": "user", "content": "Hi! How can I help you today?"}]
        agent_msgs = [{"role": "system", "content": agent_sys}, {"role": "assistant", "content": "Hi! How can I help you today?"}]
        agent_replies: List[str] = []

        with kernel.task_scope(f"tau_agent_{idx}", max_model_calls=40, max_tokens=200_000) as a_scope, \
             kernel.task_scope(f"tau_user_{idx}", max_model_calls=40, max_tokens=200_000) as u_scope:
            for step in range(14):
                u_resp = u_scope.execute(LLMQuery(model=model, messages=user_msgs, max_new_tokens=256, temperature=0.0))["response"]
                total_calls += 1
                u_usage = getattr(u_resp, "usage", None) or {}
                total_tokens += int(u_usage.get("total_tokens", 0) or 0)
                u_text = str(u_resp.response_message or "").strip()
                if u_resp.status_code != 200 or not u_text:
                    break
                user_msgs.append({"role": "assistant", "content": u_text})
                clean_u = u_text.replace("###STOP###", "").strip()
                if "###STOP###" in u_text and not clean_u:
                    break
                agent_msgs.append({"role": "user", "content": clean_u or u_text})
                if "###STOP###" in u_text and step > 0:
                    break

                for _ in range(5):
                    view = _compact_tau2_messages(agent_msgs) if resident_and_compacted else agent_msgs
                    a_resp = a_scope.execute(LLMQuery(model=model, messages=view, tools=TOOL_SCHEMAS, max_new_tokens=512, temperature=0.0))["response"]
                    total_calls += 1
                    a_usage = getattr(a_resp, "usage", None) or {}
                    total_prompt_tokens += int(a_usage.get("prompt_tokens", 0) or 0)
                    total_completion_tokens += int(a_usage.get("completion_tokens", 0) or 0)
                    total_tokens += int(a_usage.get("total_tokens", 0) or 0)
                    if a_resp.status_code != 200:
                        break
                    tcalls = a_resp.tool_calls or []
                    if tcalls:
                        norm = []
                        for j, tc in enumerate(tcalls):
                            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                            name = tc.get("name") or fn.get("name") or ""
                            raw_a = tc.get("parameters") if "parameters" in tc else fn.get("arguments", "{}")
                            parsed = json.loads(raw_a) if isinstance(raw_a, str) and raw_a.strip() else (raw_a if isinstance(raw_a, dict) else {})
                            norm.append({"id": tc.get("id") or f"c_{step}_{j}", "name": name, "parameters": parsed})
                        agent_msgs.append({
                            "role": "assistant", "content": a_resp.response_message or "",
                            "tool_calls": [{"id": n["id"], "type": "function", "function": {"name": n["name"], "arguments": json.dumps(n["parameters"], ensure_ascii=False)}} for n in norm],
                        })
                        for n in norm:
                            tr = a_scope.execute(ToolQuery(tool_calls=[{"name": f"tau2/{n['name']}", "parameters": n["parameters"]}]))["response"]
                            cstr = json.dumps(tr.response_message, ensure_ascii=False) if tr.status_code == 200 else f"Error: {tr.error}"
                            agent_msgs.append({"role": "tool", "tool_call_id": n["id"], "name": n["name"], "content": cstr})
                        if kit.transferred:
                            break
                    else:
                        reply = str(a_resp.response_message or "").strip()
                        agent_msgs.append({"role": "assistant", "content": reply})
                        agent_replies.append(reply)
                        user_msgs.append({"role": "user", "content": reply})
                        break
                if kit.transferred:
                    break

        golden = AirlineToolKit(deepcopy(base_db))
        for act in task.get("evaluation_criteria", {}).get("actions") or []:
            try:
                golden.dispatch(act["name"], act.get("arguments") or {})
            except Exception:
                pass
        db_ok = kit.db_hash() == golden.db_hash()
        comm_ok = all(_normalize(str(x)) in _normalize("\n".join(agent_replies)) for x in (task.get("evaluation_criteria", {}).get("communicate_info") or []))
        active_kit["kit"] = None
        return bool(db_ok and comm_ok)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            if resident_and_compacted:
                with AIOSKernelRuntime(storage_root=str(Path(tmp) / "resident"), log_mode="silent") as kernel:
                    kernel.llm.model_service = ms
                    kernel.scheduler.drf_controller.global_max_model_calls = 1000
                    kernel.scheduler.drf_controller.global_max_tokens = 5_000_000
                    for idx, task in enumerate(tasks):
                        if run_one_task(kernel, idx, task):
                            passed += 1
                    zero_residual = kernel.context_store.stats()["unique_objects"] == 0 and kernel.scheduler.drf_controller.used.cpu_milli == 0
            else:
                zero_residual = True
                for idx, task in enumerate(tasks):
                    with AIOSKernelRuntime(storage_root=str(Path(tmp) / f"cold_{idx}"), log_mode="silent") as kernel:
                        kernel.llm.model_service = ms
                        kernel.scheduler.drf_controller.global_max_model_calls = 1000
                        kernel.scheduler.drf_controller.global_max_tokens = 5_000_000
                        if run_one_task(kernel, idx, task):
                            passed += 1
    finally:
        ms.close()

    rss_after = _read_proc_mem_kib()
    return {
        "mode": "resident_task_scope_and_p3_compacted" if resident_and_compacted else "cold_per_task_uncompacted",
        "tasks": len(tasks),
        "passed": passed,
        "pass_rate": round(passed / max(1, len(tasks)), 4),
        "wall_seconds": round(time.perf_counter() - t_all, 2),
        "total_llm_calls": total_calls,
        "agent_prompt_tokens": total_prompt_tokens,
        "agent_completion_tokens": total_completion_tokens,
        "total_tokens": total_tokens,
        "rss_before_kib": rss_before["vm_rss_kib"],
        "rss_after_kib": rss_after["vm_rss_kib"],
        "vm_hwm_peak_kib": rss_after["vm_hwm_kib"],
        "zero_residual": zero_residual,
    }


def main() -> None:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY required")
    data_dir = Path("/home/tzd/tau2_airline")

    print("=== [1/2] Concurrent Multiplexed ModelService vs Bare HTTPS (4 concurrent real API calls) ===", flush=True)
    ms_conc = run_concurrent_model_service(n_calls=4, model="deepseek-chat")
    print(json.dumps(ms_conc, indent=2, ensure_ascii=False), flush=True)

    print("=== [2/2] Real tau2-bench 6-task Paired Comparison (Cold/Uncompacted vs Resident/Compacted + RSS) ===", flush=True)
    cold = _run_tau2_subset(data_dir, api_key, "deepseek-chat", limit=6, resident_and_compacted=False)
    resident = _run_tau2_subset(data_dir, api_key, "deepseek-chat", limit=6, resident_and_compacted=True)

    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "concurrent_model_service": ms_conc,
        "tau2_real_p2_p3_rss": {
            "cold_uncompacted": cold,
            "resident_compacted": resident,
            "agent_prompt_token_reduction_percent": round(
                100.0 * (1.0 - resident["agent_prompt_tokens"] / max(1, cold["agent_prompt_tokens"])), 2
            ),
            "wall_time_delta_percent": round(
                100.0 * (resident["wall_seconds"] - cold["wall_seconds"]) / max(1e-6, cold["wall_seconds"]), 2
            ),
        },
    }
    out = PROJECT_ROOT / "reports" / "real_gaps_closed_nuc.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("REAL_GAPS_CLOSED_REPORT " + json.dumps(report, indent=2, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
