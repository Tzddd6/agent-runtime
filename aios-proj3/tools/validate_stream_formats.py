"""
Four bounded streaming format probes (mixed Markdown, LaTeX formula, code block, long table).
Verifies incremental SSE delta fidelity across both Chat Completions SSE and Responses API SSE,
confirming SHA256(streamed_deltas) == SHA256(final_content) and zero character corruption.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from aios.llm_core.streaming import (
    parse_chat_completions_sse_stream,
    parse_responses_sse_stream,
)


STREAM_CASES: List[Tuple[str, str, int]] = [
    (
        "mixed",
        "**流式测试**\n\n| 项目 | 结果 |\n| --- | --- |\n| A | 1 |\n\n$$x^2$$\n\n```python\nprint(1)\n```",
        256,
    ),
    (
        "formula",
        "## 公式\n\n"
        r"\[F(s)=\int_0^{\infty}f(t)e^{-st}\,dt\]"
        "\n\n"
        r"\(x=\frac{1}{2}\), $a^2+b^2=c^2$."
        "\n\n"
        r"$$\begin{aligned}x+y&=3\\x-y&=1\end{aligned}$$",
        512,
    ),
    (
        "code",
        '## 代码\n\n```python\ndef greet(name):\n    return f"你好，{name}\\n"\n\nprint(greet("Runtime"))\n```\n\n```json\n{"items": [1, 2], "ok": true}\n```',
        512,
    ),
    (
        "long_table",
        "## 长表格\n\n| 序号 | 数值 |\n| --- | --- |\n"
        + "\n".join(f"| {i} | {i * i} |" for i in range(1, 41))
        + "\n\n**完成。**",
        1536,
    ),
]


def _chunk_text(text: str, step: int = 19) -> List[str]:
    return [text[i : i + step] for i in range(0, len(text), step)]


def build_chat_completions_sse_wire(target_text: str, multiline: bool = False) -> bytes:
    buf = bytearray(b"\xef\xbb\xbf: keepalive\r\n\r\n")
    # Reasoning delta that must stay private
    priv = json.dumps({"choices": [{"delta": {"reasoning_content": "PRIVATE_CHAIN_OF_THOUGHT"}}]}, ensure_ascii=False)
    buf.extend(f"data: {priv}\r\n\r\n".encode("utf-8"))

    for chunk in _chunk_text(target_text, step=17):
        obj = {"model": "deepseek-chat", "choices": [{"delta": {"content": chunk}}]}
        if multiline:
            lines = json.dumps(obj, ensure_ascii=False, indent=2).splitlines()
            wire = "event: message\r\n" + "".join(f"data: {ln}\r\n" for ln in lines) + "\r\n"
            buf.extend(wire.encode("utf-8"))
        else:
            buf.extend(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))

    final_obj = {
        "model": "deepseek-chat",
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 32, "completion_tokens": 64, "total_tokens": 96},
    }
    buf.extend(f"data: {json.dumps(final_obj, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode("utf-8"))
    return bytes(buf)


def build_responses_sse_wire(target_text: str) -> bytes:
    buf = bytearray(b": responses-stream-start\n\n")
    for chunk in _chunk_text(target_text, step=21):
        ev = {"type": "response.output_text.delta", "delta": chunk}
        buf.extend(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))

    completed_ev = {
        "type": "response.completed",
        "response": {
            "model": "gpt-6-luna",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": target_text}],
                }
            ],
            "usage": {"input_tokens": 30, "output_tokens": 60, "total_tokens": 90},
        },
    }
    buf.extend(f"data: {json.dumps(completed_ev, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode("utf-8"))
    return bytes(buf)


def run_format_validation() -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for case_name, target, max_toks in STREAM_CASES:
        for protocol in ("chat_completions_sse", "responses_sse"):
            updates: List[str] = []
            start = time.monotonic()

            def observe(event: Dict[str, Any]) -> None:
                if isinstance(event.get("text"), str):
                    updates.append(event["text"])

            if protocol == "chat_completions_sse":
                wire = build_chat_completions_sse_wire(target, multiline=(case_name == "formula"))
                parsed = parse_chat_completions_sse_stream(io.BytesIO(wire), observer=observe)
            else:
                wire = build_responses_sse_wire(target)
                parsed = parse_responses_sse_stream(io.BytesIO(wire), observer=observe)

            text = parsed["content"]
            stream_eq_final = bool(updates) and updates[-1] == text
            exact_match = text == target
            rows.append(
                {
                    "case": case_name,
                    "protocol": protocol,
                    "max_tokens": max_toks,
                    "progress_events": len(updates),
                    "stream_equals_final": stream_eq_final,
                    "exact_text": exact_match,
                    "passed": stream_eq_final and exact_match,
                    "final_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "seconds": round(time.monotonic() - start, 6),
                }
            )

    return {
        "attempts": len(rows),
        "passed": sum(1 for r in rows if r["passed"]),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON output report path")
    args = parser.parse_args()

    report = run_format_validation()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["passed"] != report["attempts"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
