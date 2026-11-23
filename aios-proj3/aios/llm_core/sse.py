"""Bounded UTF-8 SSE framing for AIOS LLM Core. Never retries or logs provider secrets."""
import codecs
from typing import Any, Callable, Iterator, Optional


def payloads(
    response: Any,
    limit: int = 1048576,
    *,
    observe: Optional[Callable[[int], None]] = None,
) -> Iterator[str]:
    """
    Incremental UTF-8 Server-Sent Events (SSE) payload generator.
    Handles:
    - UTF-8 multi-byte characters split across arbitrary chunk boundaries
    - Leading UTF-8 BOM (\\ufeff)
    - CRLF (\\r\\n), CR-only (\\r), and LF (\\n) line endings
    - Multi-line `data:` fields and `: comment` heartbeat lines
    - Strict per-event (`limit`) and total wire (`8 * limit`) byte caps
    - Unterminated trailing streams (never emitted as a false receipt)
    """
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    line: list[str] = []
    data: list[str] = []
    size = 0
    event_size = 0
    first = True
    cr = False

    read = getattr(response, "read1", None)
    if read is None:
        read = getattr(response, "read", None) or response.readline

    while True:
        chunk = read(min(limit + 1, 65536))
        if not chunk:
            decoder.decode(b"", final=True)
            return
        size += len(chunk)
        if size > 8 * limit:
            raise ValueError("SSE wire limit exceeded")
        if observe is not None:
            observe(size)
        text = decoder.decode(chunk)
        for char in text:
            if first:
                first = False
                if char == "\ufeff":
                    continue
            if char == "\n" and cr:
                cr = False
                continue
            cr = char == "\r"
            if char not in ("\r", "\n"):
                line.append(char)
                event_size += len(char.encode("utf-8"))
                if event_size > limit:
                    raise ValueError("SSE event limit exceeded")
                continue

            value = "".join(line)
            line.clear()
            if not value:
                if data:
                    yield "\n".join(data)
                data.clear()
                event_size = 0
            elif not value.startswith(":"):
                field, _, fval = value.partition(":")
                if fval.startswith(" "):
                    fval = fval[1:]
                if field == "data":
                    data.append(fval)
