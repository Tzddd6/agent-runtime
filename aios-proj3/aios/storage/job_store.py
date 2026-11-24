# Project module contract / 项目模块说明
# 职责：管理受控存储、索引和文档；外部文件内容不作为执行指令。
# 主要接口：JobStore。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
"""Atomic, bounded console job index for AIOS Kernel."""
import heapq
import json
import math
import os
from pathlib import Path
import re
import secrets
from typing import Any, Dict, List

FIELDS = {
    "id",
    "preset",
    "project",
    "status",
    "created",
    "finished",
    "workers",
    "policy",
    "model_budget",
    "context_mode",
    "states",
    "attempts",
    "context_refs",
    "results",
    "events",
    "engineering",
    "error",
}
MAX_BYTES = 512 * 1024


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, job: Dict[str, Any]) -> None:
        key = str(job["id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", key):
            raise ValueError("Invalid job identity")
        value = {k: job[k] for k in FIELDS if k in job}
        value["schema"] = 1
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("Console index exceeds bound")
        directory = self.root / key
        directory.mkdir(exist_ok=True)
        if directory.is_symlink():
            raise ValueError("Symlink job directory refused")
        target = directory / "job.json"
        temporary = directory / f".job-{secrets.token_hex(8)}"
        try:
            fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, limit: int = 30) -> List[Dict[str, Any]]:
        candidates = []
        if not self.root.exists():
            return []
        for item in self.root.iterdir():
            if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", item.name) or item.is_symlink() or not item.is_dir():
                continue
            try:
                path = item / "job.json"
                if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
                    continue
                with path.open("rb") as source:
                    raw = source.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    continue
                data = json.loads(raw)
                if (
                    data.get("schema") != 1
                    or data.get("id") != item.name
                    or type(data.get("created")) not in (int, float)
                    or not math.isfinite(data["created"])
                ):
                    continue
                value = {k: data[k] for k in FIELDS if k in data}
                if value.get("status") in ("running", "cancelling"):
                    value["status"] = "interrupted"
                    value["error"] = "服务曾中断，此记录不会自动续跑或重复调用模型"
                candidates.append(value)
                if len(candidates) > limit:
                    candidates.remove(min(candidates, key=lambda j: j["created"]))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return sorted(heapq.nlargest(limit, candidates, key=lambda j: j["created"]), key=lambda j: j["created"], reverse=True)
