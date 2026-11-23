"""
THUDM/AgentBench (~3.8k stars) OS-Interaction Subset Runner on AIOS Formal Kernel + DeepSeek.

Executes official AgentBench `data/os_interaction/data/dev.json` tasks inside isolated
Linux User + Mount + PID namespaces (`unshare -Urmpf --mount-proc`), with fresh per-task
bind mounts for `/root`, `/home`, and `/usr/local/bin`. Every model turn is dispatched
through `AIOSKernelRuntime` (`LLMQuery` -> `ModelService` -> DeepSeek), and every shell
action is audited through `AIOSKernelRuntime` (`ToolQuery`).

API keys are read strictly from `DEEPSEEK_API_KEY` in process memory and never written to disk.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from aios.llm_core.model_service import ModelService
from cerebrum.interface import AutoTool
from cerebrum.llm.apis import LLMQuery
from cerebrum.tool.apis import ToolQuery
from runtime.launch import AIOSKernelRuntime

SYSTEM_PROMPT = (
    "You are an assistant that will act like a person, I'will play the role of linux(ubuntu) operating system. "
    "Your goal is to implement the operations required by me or answer to the question proposed by me. "
    "For each of your turn, you should first think what you should do, and then take exact one of the three actions: "
    '"bash", "finish" or "answer".\n\n'
    "1. If you think you should execute some bash code, take bash action, and you should print like this:\n\n"
    "Think: put your thought here.\n\n"
    "Act: bash\n\n"
    "```bash\n"
    "# put your bash code here\n"
    "```\n\n"
    "2. If you think you have finished the task, take finish action, and you should print like this:\n\n"
    "Think: put your thought here.\n\n"
    "Act: finish\n\n"
    "3. If you think you have got the answer to the question, take answer action, and you should print like this:\n\n"
    "Think: put your thought here.\n\n"
    "Act: answer(Your answer to the question should be put in this pair of parentheses)\n\n"
    "If the output is too long, I will truncate it. The truncated output is not complete. "
    "You have to deal with the truncating problem by yourself. Attention, your bash code should not contain any input operation. "
    "Follow the exact format strictly."
)


class NamespaceSandbox:
    """Per-task Linux User+Mount+PID namespace sandbox with persistent background shell."""

    def __init__(self, scripts_root: Path):
        self.scripts_root = scripts_root
        self.tmpdir = Path(tempfile.mkdtemp(prefix="ab_os_"))
        self.fake_root = self.tmpdir / "root"
        self.fake_home = self.tmpdir / "home"
        self.fake_bin = self.tmpdir / "bin"
        self.fake_root.mkdir(mode=0o755)
        self.fake_home.mkdir(mode=0o755)
        self.fake_bin.mkdir(mode=0o755)
        # Copy existing /usr/local/bin entries if any
        ulb = Path("/usr/local/bin")
        if ulb.is_dir():
            for item in ulb.iterdir():
                dst = self.fake_bin / item.name
                try:
                    if item.is_symlink():
                        dst.symlink_to(os.readlink(item))
                    elif item.is_file():
                        shutil.copy2(item, dst)
                except OSError:
                    pass
        self.proc: Optional[subprocess.Popen] = None
        self._start_shell()

    def _start_shell(self) -> None:
        setup = (
            f"mount --bind {self.fake_root} /root && "
            f"mount --bind {self.fake_home} /home && "
            f"mount --bind {self.fake_bin} /usr/local/bin && "
            "export HOME=/root PATH=/usr/local/bin:/usr/bin:/bin && "
            "cd /root && exec /bin/bash --norc --noprofile"
        )
        self.proc = subprocess.Popen(
            ["unshare", "-Urmpf", "--mount-proc", "/bin/bash", "-c", setup],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def exec_bash(self, code: str, timeout: float = 12.0, subshell: bool = False) -> Tuple[int, str]:
        if self.proc is None or self.proc.poll() is not None:
            self._start_shell()
        assert self.proc is not None and self.proc.stdin is not None and self.proc.stdout is not None
        marker = f"__AB_DONE_{int(time.time() * 1e6)}__"
        if subshell:
            script_file = self.tmpdir / f"cmd_{int(time.time() * 1e6)}.sh"
            script_file.write_text(code + "\n", encoding="utf-8")
            script = f"/bin/bash {script_file}\n__ab_rc=$?\nprintf '\\n{marker}:%d\\n' \"$__ab_rc\"\n"
        else:
            script = f"{code}\n__ab_rc=$?\nprintf '\\n{marker}:%d\\n' \"$__ab_rc\"\n"
        try:
            self.proc.stdin.write(script)
            self.proc.stdin.flush()
        except BrokenPipeError:
            self._start_shell()
            return 1, "[SHELL_EXITED]"

        import select

        out_chunks: List[str] = []
        deadline = time.monotonic() + timeout
        rc = 0
        found = False
        fd = self.proc.stdout.fileno()
        buf = ""
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            ready, _, _ = select.select([fd], [], [], min(0.25, remaining))
            if ready:
                chunk = os.read(fd, 4096).decode("utf-8", errors="replace")
                if not chunk:
                    break
                buf += chunk
                if f"{marker}:" in buf:
                    before, after = buf.split(f"{marker}:", 1)
                    if "\n" in after:
                        rc_str = after.split("\n", 1)[0].strip()
                        try:
                            rc = int(rc_str)
                        except ValueError:
                            rc = 0
                        out_chunks.append(before)
                        found = True
                        break
        if not found:
            out_chunks.append(buf + "\n[TIMEOUT]")
            rc = 124
        text = "".join(out_chunks).strip()
        if len(text) > 2400:
            text = text[:1200] + "\n...[truncated]...\n" + text[-1000:]
        return rc, text

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except Exception:
                pass
            self.proc = None
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def parse_action(reply: str) -> Tuple[str, str]:
    """Parse AgentBench OS action: ('bash', code), ('answer', ans), or ('finish', '')."""
    text = reply or ""
    ans_match = re.search(r"Act:\s*answer\((.*)\)", text, re.DOTALL)
    if ans_match:
        return "answer", ans_match.group(1).strip()
    if re.search(r"Act:\s*finish\b", text):
        return "finish", ""
    bash_match = re.search(r"```(?:bash|sh)?\s*\n(.*?)```", text, re.DOTALL)
    if bash_match:
        return "bash", bash_match.group(1).strip()
    return "invalid", ""


def load_script_spec(spec: Any, scripts_root: Path) -> Tuple[str, str]:
    """Return (language, code) from an AgentBench script spec."""
    if isinstance(spec, str):
        return "bash", spec
    if not isinstance(spec, dict):
        return "bash", ""
    lang = spec.get("language", "bash")
    if "code" in spec:
        return lang, spec["code"]
    if "file" in spec:
        fp = scripts_root / spec["file"]
        if fp.suffix == ".py":
            lang = "python"
        return lang, fp.read_text(encoding="utf-8")
    return lang, ""


def evaluate_task(task: Dict[str, Any], sandbox: NamespaceSandbox, agent_answer: str, scripts_root: Path) -> Tuple[bool, str]:
    ev = task.get("evaluation", {})
    if "match" in ev:
        expected = str(ev["match"]).strip()
        actual = agent_answer.strip()
        return actual == expected, f"match expected={expected!r} actual={actual!r}"

    check = ev.get("check")
    example = ev.get("example")
    if isinstance(check, dict):
        lang, code = load_script_spec(check, scripts_root)
        if lang == "python":
            tmp_py = sandbox.tmpdir / "eval_check.py"
            tmp_py.write_text(code, encoding="utf-8")
            rc, out = sandbox.exec_bash(f"python3 {tmp_py}", timeout=10, subshell=True)
        else:
            rc, out = sandbox.exec_bash(code, timeout=10, subshell=True)
        return rc == 0, f"check_script rc={rc} out={out[:120]!r}"

    if isinstance(check, list) and len(check) >= 2 and isinstance(check[1], dict):
        # Run example script to get expected output, then run check[1] (e.g., integer-match.py)
        ex_lang, ex_code = load_script_spec(example, scripts_root)
        if ex_lang == "python":
            tmp_ex = sandbox.tmpdir / "eval_example.py"
            tmp_ex.write_text(ex_code, encoding="utf-8")
            _, ex_out = sandbox.exec_bash(f"python3 {tmp_ex}", timeout=10, subshell=True)
        else:
            _, ex_out = sandbox.exec_bash(ex_code, timeout=10, subshell=True)
        chk_lang, chk_code = load_script_spec(check[1], scripts_root)
        if chk_lang == "python":
            tmp_chk = sandbox.tmpdir / "eval_match.py"
            tmp_chk.write_text(chk_code, encoding="utf-8")
            p = subprocess.run(
                [sys.executable, str(tmp_chk), agent_answer.strip(), ex_out.strip()],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return p.returncode == 0, f"py_check rc={p.returncode} expected={ex_out.strip()!r} actual={agent_answer.strip()!r}"
    return False, "unsupported evaluation format"


def run_suite(
    agentbench_root: Path,
    limit: int = 20,
    max_turns: int = 8,
    model: str = "deepseek-chat",
    base_url: str = "https://api.deepseek.com/v1",
    workers: int = 4,
) -> Dict[str, Any]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY environment variable is required")

    dev_json = agentbench_root / "data" / "os_interaction" / "data" / "dev.json"
    scripts_root = agentbench_root / "data" / "os_interaction" / "scripts" / "dev"
    all_tasks: List[Dict[str, Any]] = json.loads(dev_json.read_text(encoding="utf-8"))
    tasks = all_tasks[:limit] if limit > 0 else all_tasks

    sb_lock = threading.Lock()
    sandboxes: Dict[int, NamespaceSandbox] = {}

    def _ab_bash_tool(params: Dict[str, Any]) -> Dict[str, Any]:
        sid = int(params.get("sandbox_id", 0))
        with sb_lock:
            sb = sandboxes.get(sid)
        assert sb is not None
        rc, out = sb.exec_bash(str(params.get("code", "")), timeout=12.0)
        return {"returncode": rc, "output": out}

    AutoTool.register("agentbench/bash", _ab_bash_tool, "Execute bash inside isolated AgentBench OS namespace")

    model_service = ModelService()
    model_service.request(
        "save",
        {
            "id": "deepseek_eval",
            "label": "DeepSeek Eval",
            "provider": "deepseek",
            "base_url": base_url,
            "model": model,
            "key": api_key,
        },
    )
    model_service.request("select", {"id": "deepseek_eval"})

    rows: List[Dict[str, Any]] = []
    total_llm_calls = 0
    total_tool_calls = 0
    total_tokens = 0
    t_suite0 = time.perf_counter()

    with tempfile.TemporaryDirectory() as store_tmp:
        with AIOSKernelRuntime(
            scheduler_policy="fifo",
            storage_root=str(Path(store_tmp) / "store"),
            log_mode="silent",
        ) as kernel:
            kernel.llm.model_service = model_service
            # Ensure generous model budget for benchmark run
            kernel.scheduler.drf_controller.global_max_model_calls = max(500, len(tasks) * (max_turns + 2))
            kernel.scheduler.drf_controller.global_max_tokens = 2_000_000

            stats_lock = threading.Lock()

            def _run_one(idx_task: Tuple[int, Dict[str, Any]]) -> Dict[str, Any]:
                nonlocal total_llm_calls, total_tool_calls, total_tokens
                idx, task = idx_task
                t0 = time.perf_counter()
                sandbox = NamespaceSandbox(scripts_root)
                with sb_lock:
                    sandboxes[idx] = sandbox
                agent_name = f"ab_os_agent_{idx}"
                try:
                    with kernel.task_scope(agent_name, max_model_calls=max_turns + 4, max_tokens=200_000) as scope:
                        # Init environment
                        create = task.get("create", {})
                        if isinstance(create, dict) and "init" in create:
                            init_lang, init_code = load_script_spec(create["init"], scripts_root)
                            if init_code:
                                if init_lang == "python":
                                    p = sandbox.tmpdir / "init.py"
                                    p.write_text(init_code, encoding="utf-8")
                                    sandbox.exec_bash(f"python3 {p}", timeout=10)
                                else:
                                    sandbox.exec_bash(init_code, timeout=10)
                        if task.get("start"):
                            sandbox.exec_bash(task["start"], timeout=5)
                            time.sleep(0.3)

                        messages = [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": f"Now, I will start a new problem in a new OS. My problem is:\n\n{task['description']}"},
                        ]
                        agent_answer = ""
                        turns = 0
                        status = "max_turns"
                        for turn in range(1, max_turns + 1):
                            turns = turn
                            llm_res = scope.execute(
                                LLMQuery(model=model, messages=messages, max_new_tokens=1536, temperature=0.0)
                            )
                            resp = llm_res["response"]
                            usage = getattr(resp, "usage", None) or {}
                            with stats_lock:
                                total_llm_calls += 1
                                total_tokens += int(usage.get("total_tokens", 0) or 0)
                            reply = str(resp.response_message or "")
                            if resp.status_code != 200:
                                status = f"llm_error_{resp.status_code}"
                                break
                            messages.append({"role": "assistant", "content": reply})
                            act, payload = parse_action(reply)
                            if act == "answer":
                                agent_answer = payload
                                status = "answered"
                                break
                            if act == "finish":
                                status = "finished"
                                break
                            if act == "bash":
                                tool_res = scope.execute(
                                    ToolQuery(tool_calls=[{"name": "agentbench/bash", "parameters": {"sandbox_id": idx, "code": payload}}])
                                )
                                with stats_lock:
                                    total_tool_calls += 1
                                out_obj = tool_res["response"].response_message or {}
                                obs = out_obj.get("output", "") if isinstance(out_obj, dict) else str(out_obj)
                                messages.append({"role": "user", "content": f"The output of the OS:\n\n{obs or '(empty output)'}"})
                            else:
                                messages.append(
                                    {
                                        "role": "user",
                                        "content": "Invalid action format. Reply strictly with Think + Act: bash (with ```bash block), Act: finish, or Act: answer(...).",
                                    }
                                )

                        passed, detail = evaluate_task(task, sandbox, agent_answer, scripts_root)
                        elapsed = round(time.perf_counter() - t0, 2)
                        row = {
                            "index": idx,
                            "labels": task.get("labels", []),
                            "passed": passed,
                            "status": status,
                            "turns": turns,
                            "seconds": elapsed,
                            "detail": detail,
                        }
                        print(
                            f"[{idx+1:02d}/{len(tasks)}] {'PASS' if passed else 'FAIL'} "
                            f"turns={turns} time={elapsed}s ({detail})",
                            flush=True,
                        )
                        return row
                finally:
                    with sb_lock:
                        sandboxes.pop(idx, None)
                    sandbox.close()

            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    rows = list(pool.map(_run_one, list(enumerate(tasks))))
            else:
                rows = [_run_one(item) for item in enumerate(tasks)]
            rows.sort(key=lambda r: r["index"])

            total_syscalls = kernel.syscall_executor.id
    model_service.close()

    passed_count = sum(1 for r in rows if r["passed"])
    return {
        "benchmark": "THUDM/AgentBench (~3.8k stars) OS-Interaction (dev.json)",
        "runtime": "AIOS Formal Kernel + Linux User/Mount/PID Namespace Sandbox",
        "model": model,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "tasks_evaluated": len(rows),
            "tasks_passed": passed_count,
            "pass_rate": round(passed_count / max(1, len(rows)), 4),
            "score_percent": round(100.0 * passed_count / max(1, len(rows)), 2),
            "total_llm_calls": total_llm_calls,
            "total_tool_calls": total_tool_calls,
            "total_kernel_syscalls": total_syscalls,
            "total_tokens": total_tokens,
            "wall_seconds": round(time.perf_counter() - t_suite0, 2),
        },
        "tasks": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agentbench-root", type=Path, default=Path("/home/tzd/AgentBench"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reports" / "agentbench_os_deepseek.json")
    args = parser.parse_args()

    report = run_suite(
        agentbench_root=args.agentbench_root,
        limit=args.limit,
        max_turns=args.max_turns,
        model=args.model,
        workers=args.workers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("AGENTBENCH_OS_SUMMARY " + json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
