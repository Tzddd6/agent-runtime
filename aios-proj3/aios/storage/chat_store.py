"""Bounded private conversation records & durable intent ledger for AIOS Kernel."""
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Tuple

MODES = ("auto", "llm", "team", "review", "document", "engineering", "lsfs", "litecua")
LIMIT = 8 * 1024**2


def identity(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", value):
        raise ValueError("会话和请求 ID 格式无效（需为 4~64 位字母数字或下划线）")
    return value


class ChatStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.listing_cache: Dict[str, Tuple[int, int, Dict[str, Any]]] = {}
        for path in self.root.glob("*.json"):
            try:
                chat = self.read(path.stem)
                changed = False
                for turn in chat["turns"]:
                    if turn["status"] == "running":
                        turn.update(
                            status="interrupted",
                            answer="服务曾中断，请在控制台核对原运行。此请求不会自动重试。",
                        )
                        changed = True
                if changed:
                    self.save(chat)
            except (ValueError, OSError, KeyError, TypeError):
                continue

    def read(self, key: str) -> Dict[str, Any]:
        key = identity(key)
        path = self.root / f"{key}.json"
        with self.lock:
            if path.is_symlink():
                raise ValueError("会话文件不允许符号链接")
            with path.open("rb") as f:
                raw = f.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise ValueError("会话文件超过上限")
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or value.get("id") != key
                or not isinstance(value.get("turns"), list)
                or len(value["turns"]) > 100
                or not isinstance(value.get("title"), str)
                or not isinstance(value.get("updated"), (int, float))
            ):
                raise ValueError("会话记录无效")
            stat = path.stat()
            self.listing_cache[key] = (
                stat.st_mtime_ns,
                stat.st_size,
                {"id": value["id"], "title": value["title"], "updated": value["updated"], "turns": len(value["turns"])},
            )
            return value

    def save(self, chat: Dict[str, Any]) -> None:
        key = identity(chat["id"])
        target = self.root / f"{key}.json"
        raw = json.dumps(chat, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > LIMIT:
            raise ValueError("会话记录已满，请新建对话")
        temporary = self.root / f".chat-{secrets.token_hex(8)}"
        with self.lock:
            if target.is_symlink():
                raise ValueError("会话文件不允许符号链接")
            try:
                fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, target)
                stat = target.stat()
                self.listing_cache[key] = (
                    stat.st_mtime_ns,
                    stat.st_size,
                    {"id": chat["id"], "title": chat["title"], "updated": chat["updated"], "turns": len(chat["turns"])},
                )
            finally:
                temporary.unlink(missing_ok=True)

    def listing(self) -> List[Dict[str, Any]]:
        values = []
        with self.lock:
            for p in self.root.glob("*.json"):
                try:
                    if p.is_symlink():
                        continue
                    stat = p.stat()
                    cached = self.listing_cache.get(p.stem)
                    if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                        values.append(dict(cached[2]))
                    else:
                        c = self.read(p.stem)
                        values.append({"id": c["id"], "title": c["title"], "updated": c["updated"], "turns": len(c["turns"])})
                except (ValueError, OSError, KeyError, TypeError):
                    continue
        return sorted(values, key=lambda c: c["updated"], reverse=True)

    def intent(
        self,
        key: str,
        request: str,
        prompt: str,
        mode: str = "auto",
        project: str = "pricing",
        budget: Dict[str, Any] | None = None,
        *,
        response_tokens: int = 4096,
        deep_thinking: bool = False,
        attachments: List[str] | None = None,
        use_knowledge: bool = True,
        context_mode: str = "balanced",
        job_id: str | None = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
        key = identity(key)
        request = identity(request)
        if mode not in MODES:
            raise ValueError(f"不支持的会话模式: {mode}")
        with self.lock:
            try:
                chat = self.read(key)
            except FileNotFoundError:
                if len(list(self.root.glob("*.json"))) >= 100:
                    raise ValueError("已达到 100 个会话上限，请管理员归档记录")
                chat = {"id": key, "title": prompt[:32] or "AIOS 协同任务", "updated": time.time(), "turns": []}
            signature = {
                "prompt": prompt,
                "mode": mode,
                "project": project,
                "model_budget": budget or {"calls": 32, "output_tokens": 16384},
                "response_tokens": response_tokens,
                "deep_thinking": deep_thinking,
                "attachments": attachments or [],
                "use_knowledge": use_knowledge,
                "context_mode": context_mode,
            }
            for turn in chat["turns"]:
                if turn["request_id"] == request:
                    for k, v in signature.items():
                        if turn.get(k) != v:
                            raise ValueError("同一请求 ID 不允许更改内容")
                    return chat, turn, True
            if len(chat["turns"]) >= 100:
                raise ValueError("当前会话已达 100 轮，请归档后新建对话")
            turn = {
                **signature,
                "request_id": request,
                "job_id": job_id,
                "syscall_id": None,
                "created": time.time(),
                "status": "running",
                "answer": "",
                "citations": [],
            }
            chat["turns"].append(turn)
            chat["updated"] = time.time()
            self.save(chat)
            return chat, turn, False

    def update(self, key: str, request: str, **changes: Any) -> Dict[str, Any]:
        key = identity(key)
        request = identity(request)
        with self.lock:
            chat = self.read(key)
            turn = next(t for t in chat["turns"] if t["request_id"] == request)
            turn.update(changes)
            chat["updated"] = time.time()
            self.save(chat)
            return turn
