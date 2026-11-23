"""
Trusted SSE Streaming & Provider Protocol Engine for AIOS LLM Core.
Supports both:
1. OpenAI / DeepSeek Chat Completions SSE (`/chat/completions`)
2. OpenAI / Relay Responses API SSE (`/responses`)
Enforces strict privacy: reasoning tokens and supplier keys are never emitted to observers
or logs, and uncertain interrupted streams are never automatically replayed.
"""
import hashlib
import io
import json
import time
from typing import Any, Callable, Dict, List, Optional
import urllib.error
import urllib.request

from aios.llm_core.sse import payloads

MAX_BYTES = 1048576  # 1 MiB safety ceiling


class ModelStreamError(RuntimeError):
    def __init__(self, message: str, *, code: str = "MODEL_STREAM_ERROR", metadata: Optional[Dict[str, int]] = None):
        super().__init__(message)
        self.code = code
        self.metadata = metadata or {"error_kind": 0, "error_param": 0}


def error_metadata(value: Any) -> Dict[str, int]:
    """Return fixed numeric categories only; never expose arbitrary upstream error text or keys."""
    if not isinstance(value, dict):
        return {"error_kind": 0, "error_param": 0}
    error = value.get("error", value)
    if not isinstance(error, dict):
        return {"error_kind": 0, "error_param": 0}
    code = error.get("code") or error.get("type")
    kinds = {
        "rate_limit_exceeded": 1,
        "server_error": 2,
        "timeout": 3,
        "invalid_request_error": 4,
        "insufficient_quota": 5,
        "context_length_exceeded": 6,
        "model_not_found": 7,
        "unsupported_parameter": 8,
        "invalid_value": 9,
        "invalid_api_key": 10,
    }
    params = {
        "input": 1,
        "messages": 2,
        "max_output_tokens": 3,
        "reasoning": 4,
        "reasoning.effort": 5,
        "tools": 6,
        "tool_choice": 7,
        "model": 8,
        "store": 9,
        "stream": 10,
    }
    return {
        "error_kind": kinds.get(code, 0) if isinstance(code, str) else 0,
        "error_param": params.get(error.get("param"), 0) if isinstance(error.get("param"), str) else 0,
    }


def encode_responses_input(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    instructions: List[str] = []
    items: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role in ("system", "developer"):
            if not isinstance(content, str):
                raise ValueError("Invalid system instructions")
            instructions.append(content)
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id", ""),
                    "output": content or "",
                }
            )
            continue
        if content:
            if isinstance(content, str):
                parts = [{"type": "output_text" if role == "assistant" else "input_text", "text": content}]
            elif isinstance(content, list) and role == 'user':
                parts = []
                for part in content:
                    if part.get('type') == 'text':
                        parts.append({'type': 'input_text', 'text': part['text']})
                    elif part.get('type') == 'image_url':
                        parts.append({'type': 'input_image', 'image_url': part['image_url']['url']})
                    else:
                        raise ValueError('Unsupported multimodal part')
            else:
                raise ValueError("Unsupported message content")
            items.append({"type": "message", "role": role, "content": parts})
        if role == 'assistant':
            for call in m.get('tool_calls') or []:
                function = call['function']
                items.append({'type': 'function_call', 'call_id': call['id'],
                              'name': function['name'], 'arguments': function['arguments']})
    if not items:
        raise ValueError("Missing Responses input")
    result: Dict[str, Any] = {
        "input": items,
        "instructions": "\n\n".join(instructions) or "Follow the user request and use available tools when needed.",
    }
    if tools:
        result["tools"] = [{"type": "function", **t["function"]} for t in tools if "function" in t]
    return result


def decode_responses_output(value: Dict[str, Any], default_model: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("status") not in ("completed", "incomplete"):
        raise ModelStreamError("Relay Responses did not complete", code="MODEL_UPSTREAM_FAILED")
    text_parts: List[str] = []
    calls: List[Dict[str, Any]] = []
    for item in value.get("output", []):
        if item.get("type") == "message" and item.get("role") == "assistant":
            for part in item.get("content", []):
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
        elif item.get("type") == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if not all(isinstance(v, str) and v for v in (call_id, name, arguments)):
                raise ModelStreamError("Invalid Responses tool call")
            calls.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}})

    body = "".join(text_parts)
    if not body.strip() and not calls:
        raise ModelStreamError("Relay returned no final assistant output")

    usage = value.get("usage") or {}
    norm_usage: Dict[str, Any] = {}
    for src, dst in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        num = usage.get(src)
        if type(num) is not int or num < 0:
            raise ModelStreamError("Missing Responses usage")
        norm_usage[dst] = num

    finish = "length" if value["status"] == "incomplete" else "tool_calls" if calls else "stop"
    return {
        "content": body,
        "tool_calls": calls or None,
        "usage": norm_usage,
        "model": value.get("model", default_model),
        "finish_reason": finish,
    }


def parse_chat_completions_sse_stream(
    stream_source: Any,
    default_model: str = "deepseek-chat",
    observer: Optional[Callable[[Dict[str, Any]], None]] = None,
    limit: int = MAX_BYTES,
) -> Dict[str, Any]:
    """
    Parse a Chat Completions `text/event-stream` response body (`stream_source`).
    Emits live content deltas to `observer` (never emitting `reasoning_content`),
    and verifies complete termination (`[DONE]` + `finish_reason`).
    """
    started = time.monotonic()
    parts: List[str] = []
    usage: Dict[str, Any] = {}
    finish_reason: Optional[str] = None
    model = default_model
    output_bytes = 0
    done = False
    deltas_emitted = 0
    tool_parts = {}

    for payload in payloads(stream_source, limit):
        if payload == "[DONE]":
            done = True
            break
        chunk = json.loads(payload)
        model = chunk.get("model", model)
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            delta_obj = choice.get("delta", {})
            for call in delta_obj.get('tool_calls') or []:
                index = call.get('index')
                if type(index) is not int or not 0 <= index < 8:
                    raise ModelStreamError('Invalid tool call index')
                item = tool_parts.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                for key, value in [('id', call.get('id')), ('name', (call.get('function') or {}).get('name')),
                                   ('arguments', (call.get('function') or {}).get('arguments'))]:
                    if value is None:
                        continue
                    if not isinstance(value, str):
                        raise ModelStreamError('Invalid tool call fragment')
                    target = item if key == 'id' else item['function']
                    target[key] += value
                    if len(target[key]) > (16000 if key == 'arguments' else 256):
                        raise ModelStreamError('Tool call exceeds limit')
            # Never emit delta_obj.get('reasoning_content') to observer
            delta = delta_obj.get("content")
            if delta is not None:
                if not isinstance(delta, str):
                    raise ModelStreamError("Invalid text delta")
                parts.append(delta)
                output_bytes += len(delta.encode("utf-8"))
                if output_bytes > limit:
                    raise ModelStreamError("Text exceeds limit")
                if delta:
                    deltas_emitted += 1
                    if observer is not None:
                        observer({"source": "provider_content_delta", "delta": delta, "text": "".join(parts)})
            if choice.get("finish_reason") is not None:
                finish_reason = choice["finish_reason"]

    full_text = "".join(parts)
    calls = [tool_parts[index] for index in sorted(tool_parts)]
    if any(not call['id'] or not call['function']['name'] or not call['function']['arguments'] for call in calls):
        raise ModelStreamError('Incomplete tool call')
    if not done or finish_reason not in ('stop', 'tool_calls') or not (full_text.strip() or calls):
        raise ModelStreamError("Streaming output incomplete; no retry", code="MODEL_STREAM_INCOMPLETE")

    return {
        "content": full_text,
        "tool_calls": calls or None,
        "model": model,
        "usage": usage,
        "finish_reason": finish_reason,
        "stream_deltas": deltas_emitted,
        "latency_seconds": round(time.monotonic() - started, 6),
    }


def parse_responses_sse_stream(
    stream_source: Any,
    default_model: str = "gpt-6-luna",
    observer: Optional[Callable[[Dict[str, Any]], None]] = None,
    limit: int = MAX_BYTES,
) -> Dict[str, Any]:
    """
    Parse a Responses API `text/event-stream` response body (`stream_source`).
    Handles `response.output_text.delta` and `response.completed`, verifying that
    the SHA256 hash of streamed deltas matches the final completed assistant text.
    """
    started = time.monotonic()
    text_bytes = 0
    text_hash = hashlib.sha256()
    accumulated: List[str] = []
    response_value: Optional[Dict[str, Any]] = None
    deltas_emitted = 0

    for payload in payloads(stream_source, limit):
        if payload == "[DONE]":
            break
        event = json.loads(payload)
        if not isinstance(event, dict):
            raise ModelStreamError("Responses event must be an object")
        kind = event.get("type")
        if kind == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise ModelStreamError("Invalid Responses delta")
            encoded = delta.encode("utf-8")
            text_bytes += len(encoded)
            if text_bytes > limit:
                raise ModelStreamError("Responses text exceeds limit")
            text_hash.update(encoded)
            accumulated.append(delta)
            deltas_emitted += 1
            if observer is not None:
                observer({"source": "provider_content_delta", "delta": delta, "text": "".join(accumulated)})
        elif kind in ("response.completed", "response.incomplete"):
            response_value = event.get("response")
            break
        elif kind in ("response.failed", "error") or event.get("error"):
            failed_resp = event.get("response") if isinstance(event.get("response"), dict) else {}
            raw_err = event.get("error") or failed_resp.get("error") or event
            raise ModelStreamError(
                "Responses provider rejected request",
                code="MODEL_UPSTREAM_FAILED",
                metadata=error_metadata(raw_err),
            )

    if response_value is None:
        raise ModelStreamError("Responses stream incomplete; no retry", code="MODEL_UPSTREAM_FAILED")

    result = decode_responses_output(response_value, default_model)
    if text_bytes and text_hash.hexdigest() != hashlib.sha256(result["content"].encode("utf-8")).hexdigest():
        raise ModelStreamError("Responses final text differs from stream", code="MODEL_STREAM_MISMATCH")

    result["stream_deltas"] = deltas_emitted
    result["latency_seconds"] = round(time.monotonic() - started, 6)
    return result
