# Project module contract / 项目模块说明
# 职责：管理记忆预算、检索与注入；跨Agent/用户访问以显式身份及策略为界。
# 主要接口：MemoryWriteBarrier。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple


class MemoryWriteBarrier:
    """
    Per-user_id write barrier ensuring read-after-write consistency across
    concurrent AIOS Kernel queues (Memory Queue vs LLM Queue).
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        self.enabled: bool = bool(cfg.get("enabled", True))
        self.timeout_seconds: float = float(cfg.get("timeout_seconds", 5.0))
        self._cond = threading.Condition()
        self._seq_by_user: Dict[str, int] = {}
        self._pending_by_user: Dict[str, Dict[str, int]] = {}

    def register_write(self, user_id: str) -> Tuple[str, int]:
        uid = user_id or "default"
        token = uuid.uuid4().hex
        with self._cond:
            seq = self._seq_by_user.get(uid, 0) + 1
            self._seq_by_user[uid] = seq
            self._pending_by_user.setdefault(uid, {})[token] = seq
            return token, seq

    def complete_write(self, user_id: str, token: str) -> None:
        uid = user_id or "default"
        with self._cond:
            pending = self._pending_by_user.get(uid)
            if pending and token in pending:
                pending.pop(token, None)
            self._cond.notify_all()

    def snapshot_seq(self, user_id: str) -> int:
        uid = user_id or "default"
        with self._cond:
            return self._seq_by_user.get(uid, 0)

    def wait_for_seq(self, user_id: str, target_seq: Optional[int]) -> bool:
        if not self.enabled or not target_seq or target_seq <= 0:
            return True
        uid = user_id or "default"
        deadline = time.monotonic() + self.timeout_seconds
        with self._cond:
            while True:
                pending = self._pending_by_user.get(uid) or {}
                blocking = any(seq <= target_seq for seq in pending.values())
                if not blocking:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)
