"""
Bare Direct-HTTPS Control Group Runner (ZERO AIOS Kernel, ZERO ModelService, ZERO TaskScope)
for strict per-task failure attribution against AIOS Runtime across all 4 GitHub benchmarks
that had any failed tasks:
  1. ShishirPatil/gorilla BFCL v4 Parallel (30 tasks)
  2. google-research/mbpp (20 tasks)
  3. THUDM/AgentBench OS Interaction (26 tasks)
  4. sierra-research/tau2-bench Airline (20 tasks)

API key is read strictly from `DEEPSEEK_API_KEY` in process memory and never written to disk.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.benchmark_agentbench_os import (
    NamespaceSandbox,
    SYSTEM_PROMPT as AB_SYSTEM_PROMPT,
    evaluate_task as ab_grade,
    load_script_spec,
    parse_action as ab_parse,
)
from tools.benchmark_expanded_github_suite import (
    _bfcl_to_openai_tools,
    _extract_python_code,
    _grade_bfcl_parallel,
    _run_isolated_python_test,
)
from tools.benchmark_tau2_airline import AirlineToolKit, TOOL_SCHEMAS as TAU2_TOOLS, _normalize as tau2_norm


def bare_chat_completion(
    api_key: str,
    messages: List[Dict[str, Any]],
    model: str = "deepseek-chat",
    max_tokens: int = 512,
    temperature: float = 0.0,
    tools: Optional[List[Dict[str, Any]]] = None,
    timeout: float = 45.0,
) -> Tuple[int, str, List[Dict[str, Any]], str]:
    """Direct HTTPS POST to DeepSeek API without any AIOS Kernel / ModelService / scheduler."""
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        msg = raw["choices"][0]["message"]
        return 200, str(msg.get("content") or ""), list(msg.get("tool_calls") or []), ""
    except urllib.error.HTTPError as exc:
        return exc.code, "", [], f"HTTP {exc.code}"
    except Exception as exc:
        return 502, "", [], f"{type(exc).__name__}: {exc}"


def _save(report: Dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. BFCL v4 Parallel (30 tasks) — Bare Direct HTTPS
# ---------------------------------------------------------------------------
def run_bare_bfcl(api_key: str, ext_dir: Path, limit: int = 30) -> Dict[str, Any]:
    print(f"=== [BARE 1/4] BFCL v4 Parallel ({limit} tasks) ===", flush=True)
    items = [json.loads(line) for line in (ext_dir / "BFCL_v4_parallel.json").read_text(encoding="utf-8").splitlines()[:limit]]
    ans_map = {
        json.loads(line)["id"]: json.loads(line)["ground_truth"]
        for line in (ext_dir / "BFCL_v4_parallel_ans.json").read_text(encoding="utf-8").splitlines()
    }
    t0 = time.perf_counter()

    def _one(item: Dict[str, Any]) -> Dict[str, Any]:
        tools, safe_to_orig = _bfcl_to_openai_tools(item["function"])
        status, _, tcs, err = bare_chat_completion(api_key, item["question"][0], max_tokens=512, tools=tools)
        ok = status == 200 and _grade_bfcl_parallel(tcs, ans_map[item["id"]], safe_to_orig)
        return {"id": item["id"], "passed": ok, "status": status, "error": err, "calls_count": len(tcs), "gt_count": len(ans_map[item["id"]])}

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(_one, items))
    wall = round(time.perf_counter() - t0, 2)
    passed = sum(1 for r in rows if r["passed"])
    fails = [r for r in rows if not r["passed"]]
    print(f"  BARE BFCL: {passed}/{limit} ({100*passed/limit:.1f}%) wall={wall}s fails={[f['id'] for f in fails]}", flush=True)
    return {"tasks": limit, "passed": passed, "accuracy": round(passed / limit, 4), "wall_seconds": wall, "failed_tasks": fails}


# ---------------------------------------------------------------------------
# 2. MBPP (20 tasks) — Bare Direct HTTPS
# ---------------------------------------------------------------------------
def run_bare_mbpp(api_key: str, ext_dir: Path, limit: int = 20) -> Dict[str, Any]:
    print(f"=== [BARE 2/4] google-research/mbpp ({limit} tasks) ===", flush=True)
    raw = [json.loads(line) for line in (ext_dir / "mbpp.jsonl").read_text(encoding="utf-8").splitlines()]
    tasks = raw[10 : 10 + limit]
    t0 = time.perf_counter()

    def _one(idx_t: Tuple[int, Dict[str, Any]]) -> Dict[str, Any]:
        idx, t = idx_t
        prompt = f"{t['text']}\n\nYour code must satisfy these test cases:\n" + "\n".join(t["test_list"])
        status, text, _, err = bare_chat_completion(
            api_key,
            [
                {"role": "system", "content": "You are an expert Python engineer. Output only valid Python code in a single ```python``` block."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=768,
        )
        if status != 200:
            return {"idx": idx, "task_id": t["task_id"], "passed": False, "error": err}
        code = _extract_python_code(text)
        ok, test_err = _run_isolated_python_test(f"{code}\n\n" + "\n".join(t["test_list"]) + "\n")
        return {"idx": idx, "task_id": t["task_id"], "passed": ok, "error": test_err[:240] if not ok else ""}

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(_one, list(enumerate(tasks))))
    wall = round(time.perf_counter() - t0, 2)
    passed = sum(1 for r in rows if r["passed"])
    fails = [r for r in rows if not r["passed"]]
    print(f"  BARE MBPP: {passed}/{limit} ({100*passed/limit:.1f}%) wall={wall}s fails={[f['task_id'] for f in fails]}", flush=True)
    return {"tasks": limit, "passed": passed, "accuracy": round(passed / limit, 4), "wall_seconds": wall, "failed_tasks": fails}


# ---------------------------------------------------------------------------
# 3. THUDM/AgentBench OS (26 tasks) — Bare Direct HTTPS
# ---------------------------------------------------------------------------
def run_bare_agentbench(api_key: str, ab_root: Path, limit: int = 26) -> Dict[str, Any]:
    print(f"=== [BARE 3/4] THUDM/AgentBench OS ({limit} tasks) ===", flush=True)
    data_dir = ab_root / "data" / "os_interaction" / "data"
    scripts_root = ab_root / "data" / "os_interaction" / "scripts" / "dev"
    tasks = json.loads((data_dir / "dev.json").read_text(encoding="utf-8"))[:limit]
    t0 = time.perf_counter()
    rows = []

    for idx, task in enumerate(tasks):
        t_task = time.perf_counter()
        sb = NamespaceSandbox(scripts_root)
        answer_text = ""
        err_note = ""
        try:
            create = task.get("create", {})
            if isinstance(create, dict) and "init" in create:
                init_lang, init_code = load_script_spec(create["init"], scripts_root)
                if init_code:
                    if init_lang == "python":
                        p = sb.tmpdir / "init.py"
                        p.write_text(init_code, encoding="utf-8")
                        sb.exec_bash(f"python3 {p}", timeout=10)
                    else:
                        sb.exec_bash(init_code, timeout=10)
            if task.get("start"):
                sb.exec_bash(task["start"], timeout=5)
                time.sleep(0.3)
            msgs = [
                {"role": "system", "content": AB_SYSTEM_PROMPT},
                {"role": "user", "content": f"Now, I will start a new problem in a new OS. My problem is:\n\n{task['description']}"},
            ]
            for _ in range(8):
                status, reply, _, err = bare_chat_completion(api_key, msgs, max_tokens=512)
                if status != 200 or not reply:
                    err_note = f"http_{status}:{err}"
                    break
                msgs.append({"role": "assistant", "content": reply})
                act, payload = ab_parse(reply)
                if act == "bash":
                    out = sb.exec_bash(payload, timeout=12)
                    msgs.append({"role": "user", "content": f"The output of the OS:\n\n{out}"})
                elif act == "answer":
                    answer_text = payload
                    break
                elif act == "finish":
                    break
                else:
                    msgs.append({"role": "user", "content": "Invalid action format. Please use Act: bash, Act: finish, or Act: answer(...)."})
            passed, detail = ab_grade(task, sb, answer_text, scripts_root)
        finally:
            sb.close()
        if err_note and not passed:
            detail = f"{err_note} | {detail}"
        rows.append({"index": idx, "passed": passed, "detail": detail, "seconds": round(time.perf_counter() - t_task, 2)})
        print(f"  [BARE AB {idx+1:02d}/{len(tasks)}] {'PASS' if passed else 'FAIL'} ({detail[:90]})", flush=True)

    wall = round(time.perf_counter() - t0, 2)
    passed_cnt = sum(1 for r in rows if r["passed"])
    print(f"  BARE AgentBench OS: {passed_cnt}/{len(rows)} ({100*passed_cnt/len(rows):.1f}%) wall={wall}s", flush=True)
    return {"tasks": len(rows), "passed": passed_cnt, "pass_rate": round(passed_cnt / len(rows), 4), "wall_seconds": wall, "rows": rows}


# ---------------------------------------------------------------------------
# 4. sierra-research/tau2-bench Airline (20 tasks) — Bare Direct HTTPS
# ---------------------------------------------------------------------------
def run_bare_tau2(api_key: str, data_dir: Path, limit: int = 20) -> Dict[str, Any]:
    print(f"=== [BARE 4/4] sierra-research/tau2-bench Airline ({limit} tasks) ===", flush=True)
    base_db = json.loads((data_dir / "db.json").read_text(encoding="utf-8"))
    policy = (data_dir / "policy.md").read_text(encoding="utf-8")
    tasks = json.loads((data_dir / "tasks.json").read_text(encoding="utf-8"))[:limit]
    t0 = time.perf_counter()
    rows = []

    for idx, task in enumerate(tasks):
        t_task = time.perf_counter()
        kit = AirlineToolKit(deepcopy(base_db))
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
        term = "max_steps"

        for _ in range(16):
            st_u, u_text, _, err_u = bare_chat_completion(api_key, user_msgs, max_tokens=256)
            if st_u != 200 or not u_text.strip():
                term = f"user_err_{st_u}"
                break
            user_msgs.append({"role": "assistant", "content": u_text})
            clean_u = u_text.replace("###STOP###", "").strip()
            if clean_u:
                agent_msgs.append({"role": "user", "content": clean_u})
            if "###STOP###" in u_text:
                term = "user_stop"
                break

            transferred = False
            for _ in range(6):
                st_a, a_text, tcs, err_a = bare_chat_completion(api_key, agent_msgs, max_tokens=512, tools=TAU2_TOOLS)
                if st_a != 200:
                    term = f"agent_err_{st_a}"
                    transferred = True
                    break
                if tcs:
                    agent_msgs.append({"role": "assistant", "content": a_text or None, "tool_calls": tcs})
                    for tc in tcs:
                        fn = tc.get("function") or {}
                        tname = fn.get("name", "")
                        try:
                            targs = json.loads(fn.get("arguments") or "{}")
                        except json.JSONDecodeError:
                            targs = {}
                        tout = kit.dispatch(tname, targs)
                        agent_msgs.append({"role": "tool", "tool_call_id": tc.get("id", "tc"), "content": str(tout)})
                        if tname == "transfer_to_human_agents":
                            term = "transferred"
                            transferred = True
                    if transferred:
                        break
                else:
                    agent_msgs.append({"role": "assistant", "content": a_text})
                    agent_replies.append(a_text)
                    user_msgs.append({"role": "user", "content": a_text})
                    break
            if transferred:
                break

        # Official tau2-bench DB hash + communicate_info grading
        expected_kit = AirlineToolKit(deepcopy(base_db))
        for act in (task.get("evaluation_criteria", {}).get("actions") or []):
            expected_kit.dispatch(act["name"], act.get("arguments") or {})
        db_match = kit.db_hash() == expected_kit.db_hash()
        comm_req = task.get("evaluation_criteria", {}).get("communicate_info") or []
        joined = tau2_norm(" ".join(agent_replies))
        comm_match = all(tau2_norm(str(c)) in joined for c in comm_req)
        passed = db_match and comm_match
        rows.append({
            "task_id": str(task["id"]),
            "passed": passed,
            "db_match": db_match,
            "comm_match": comm_match,
            "terminated": term,
            "seconds": round(time.perf_counter() - t_task, 2),
        })
        print(f"  [BARE TAU2 {idx+1:02d}/{len(tasks)}] id={task['id']} {'PASS' if passed else 'FAIL'} db={db_match} comm={comm_match} term={term}", flush=True)

    wall = round(time.perf_counter() - t0, 2)
    passed_cnt = sum(1 for r in rows if r["passed"])
    print(f"  BARE tau2-bench: {passed_cnt}/{len(rows)} ({100*passed_cnt/len(rows):.1f}%) wall={wall}s", flush=True)
    return {"tasks": len(rows), "passed": passed_cnt, "pass_1_rate": round(passed_cnt / len(rows), 4), "wall_seconds": wall, "rows": rows}


def main() -> None:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY required")
    ext_dir = Path("/home/tzd/ext_benchmarks")
    out_path = PROJECT_ROOT / "reports" / "bare_control_attribution_nuc.json"
    if out_path.exists():
        report: Dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8"))
    else:
        report = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": "BARE_DIRECT_HTTPS_NO_AIOS"}

    if "bfcl_v4_parallel_30" not in report:
        report["bfcl_v4_parallel_30"] = run_bare_bfcl(api_key, ext_dir, limit=30)
        _save(report, out_path)

    if "mbpp_20" not in report:
        report["mbpp_20"] = run_bare_mbpp(api_key, ext_dir, limit=20)
        _save(report, out_path)

    if "agentbench_os_26" not in report:
        report["agentbench_os_26"] = run_bare_agentbench(api_key, Path("/home/tzd/AgentBench"), limit=26)
        _save(report, out_path)

    if "tau2_airline_20" not in report:
        report["tau2_airline_20"] = run_bare_tau2(api_key, Path("/home/tzd/tau2_airline"), limit=20)
        _save(report, out_path)

    print("BARE_CONTROL_ALL_DONE " + str(out_path), flush=True)


if __name__ == "__main__":
    main()
