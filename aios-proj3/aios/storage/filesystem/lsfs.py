from collections import defaultdict
import hashlib
import os
import shutil
import threading
import time
import uuid
from typing import Any, Dict, List


class LSFS:
    """
    LLM-based Semantic File System (LSFS) for AIOS StorageManager (ICLR 2025: From Commands to Prompts).
    Provides sandboxed file operations within `root_dir`, version history for `rollback(file_path, n)`,
    secure file sharing links (`share`), and keyword/semantic document retrieval (`retrieve`).
    """

    def __init__(self, root_dir: str, use_vector_db: bool = False):
        self.root_dir = os.path.abspath(root_dir)
        self.use_vector_db = use_vector_db
        os.makedirs(self.root_dir, exist_ok=True)
        self._versions: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._shares: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def _resolve_path(self, rel_path: str) -> str:
        clean = (rel_path or "").lstrip("/\\")
        target = os.path.abspath(os.path.join(self.root_dir, clean))
        from pathlib import Path
        for part in (Path(target), *Path(target).parents):
            if str(part) == self.root_dir:
                break
            if part.is_symlink():
                raise ValueError('Symbolic links are not allowed in storage paths')
        target = os.path.realpath(target)
        if os.path.commonpath([self.root_dir, target]) != self.root_dir:
            raise ValueError(f"Path traversal outside storage root refused: {rel_path}")
        return target

    def _record_version(self, target_path: str, content: str) -> None:
        self._versions[target_path].append(
            {"content": content, "timestamp": time.time()}
        )

    def address_request(self, syscall: Any) -> Any:
        query = getattr(syscall, "query", syscall)
        op = getattr(query, "operation_type", "read")
        params: Dict[str, Any] = getattr(query, "params", {}) or {}

        with self._lock:
            if op == "create_dir":
                dir_path = self._resolve_path(params.get("dir_path") or params.get("path") or "")
                os.makedirs(dir_path, exist_ok=True)
                return f"Directory created: {os.path.relpath(dir_path, self.root_dir)}"

            if op == "create_file":
                file_path = self._resolve_path(params.get("file_path") or params.get("path") or "")
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                if not os.path.exists(file_path):
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write("")
                    self._record_version(file_path, "")
                return f"File created: {os.path.relpath(file_path, self.root_dir)}"

            if op == "write":
                file_path = self._resolve_path(params.get("file_path") or params.get("path") or "")
                content = str(params.get("content", ""))
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(content)
                self._record_version(file_path, content)
                return f"Written {len(content)} chars to {os.path.relpath(file_path, self.root_dir)}"

            if op == "read":
                file_path = self._resolve_path(params.get("file_path") or params.get("path") or "")
                if not os.path.exists(file_path):
                    raise FileNotFoundError(f"File not found: {params.get('file_path') or params.get('path')}")
                with open(file_path, "r", encoding="utf-8") as f:
                    return f.read()

            if op == "delete_file":
                file_path = self._resolve_path(params.get("file_path") or params.get("path") or "")
                if os.path.exists(file_path):
                    os.remove(file_path)
                    return f"Deleted file: {os.path.relpath(file_path, self.root_dir)}"
                return "File does not exist"

            if op == "delete_dir":
                dir_path = self._resolve_path(params.get("dir_path") or params.get("path") or "")
                if dir_path == self.root_dir:
                    raise ValueError("Cannot delete storage root directory")
                if os.path.exists(dir_path):
                    shutil.rmtree(dir_path)
                    return f"Deleted directory: {os.path.relpath(dir_path, self.root_dir)}"
                return "Directory does not exist"

            if op == "rollback":
                file_path = self._resolve_path(params.get("file_path") or params.get("path") or "")
                steps = int(params.get("n", 1))
                history = self._versions.get(file_path, [])
                if len(history) <= steps:
                    raise ValueError(f"Not enough version history to rollback {steps} step(s)")
                for _ in range(steps):
                    history.pop()
                restored = history[-1]["content"]
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(restored)
                return restored

            if op == "list":
                dir_path = self._resolve_path(params.get("dir_path") or params.get("path") or "")
                if not os.path.exists(dir_path):
                    return []
                return sorted(os.listdir(dir_path))

            if op == "retrieve":
                query_text = str(params.get("query_text") or params.get("query") or "")
                keywords_raw = str(params.get("keywords") or "")
                k = int(params.get("k", 3))
                terms = [t.lower() for t in (query_text + " " + keywords_raw).split() if t.strip()]
                required_kw = [kw.lower() for kw in keywords_raw.split() if kw.strip()]

                matches: List[Dict[str, Any]] = []
                for walk_root, _, files in os.walk(self.root_dir):
                    for fname in files:
                        fpath = os.path.join(walk_root, fname)
                        rel = os.path.relpath(fpath, self.root_dir).replace("\\", "/")
                        try:
                            with open(fpath, "r", encoding="utf-8") as f:
                                text = f.read()
                        except Exception:
                            continue
                        low = (rel + "\n" + text).lower()
                        if required_kw and not all(kw in low for kw in required_kw):
                            continue
                        score = sum(low.count(t) for t in terms) if terms else 1
                        if score > 0:
                            matches.append(
                                {
                                    "file_path": rel,
                                    "score": score,
                                    "snippet": text[:200],
                                }
                            )
                matches.sort(key=lambda item: item["score"], reverse=True)
                return matches[: max(1, k)]

            if op == "share":
                file_path = self._resolve_path(params.get("file_path") or params.get("path") or "")
                if not os.path.exists(file_path):
                    raise FileNotFoundError(f"Cannot share non-existent file: {params.get('file_path')}")
                with open(file_path, "rb") as f:
                    digest = hashlib.sha256(f.read()).hexdigest()[:16]
                token = f"share_{uuid.uuid4().hex[:10]}_{digest}"
                rel = os.path.relpath(file_path, self.root_dir).replace("\\", "/")
                share_meta = {
                    "share_link": f"lsfs://share/{token}",
                    "token": token,
                    "file_path": rel,
                    "sha256_prefix": digest,
                    "shared_with": params.get("target_agent", "public"),
                    "expires_at": time.time() + float(params.get("ttl_seconds", 3600)),
                }
                self._shares[token] = share_meta
                return share_meta

            raise ValueError(f"Unsupported storage operation: {op}")
