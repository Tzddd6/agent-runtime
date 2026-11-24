# Project module contract / 项目模块说明
# 职责：管理上下文快照、内容对象与读取预算；文本引用复用不是远端GPU KV Cache零拷贝。
# 主要接口：SimpleContextManager。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from aios.context.base import BaseContextManager


class SimpleContextManager(BaseContextManager):
    """
    Context Manager for AIOS Kernel LLM Core and Round-Robin Scheduler.
    Handles snapshotting and restoring in-flight LLM generation states across
    time-slice preemption boundaries.
    """

    def __init__(self):
        super().__init__()
        self.context_dict: Dict[int, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def check_restoration(self, pid: int) -> bool:
        with self._lock:
            return pid in self.context_dict

    def load_context(self, pid: int, *args, **kwargs) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.context_dict.get(pid)

    def clear_restoration(self, pid: int) -> None:
        with self._lock:
            self.context_dict.pop(pid, None)

    def save_context(
        self,
        pid: int,
        model_name: str,
        messages: List[Dict[str, Any]],
        token_stream: Iterable[str],
        time_limit: Optional[float] = None,
        per_token_delay: float = 0.0,
    ) -> Tuple[str, bool]:
        """
        Consume tokens from `token_stream` up to `time_limit` seconds.
        If `time_limit` expires before the stream finishes, snapshot the accumulated
        prefix and remaining iterator in `self.context_dict[pid]` and return
        `(partial_response, False)`. When completed, clear snapshot and return
        `(full_response, True)`.
        """
        saved = self.load_context(pid)
        if saved is not None:
            accumulated: List[str] = list(saved.get("accumulated", []))
            iterator = saved.get("iterator")
        else:
            accumulated = []
            iterator = iter(token_stream)

        start_ts = time.monotonic()
        finished = True

        for token in iterator:
            if per_token_delay > 0:
                time.sleep(per_token_delay)
            accumulated.append(token)
            if time_limit is not None and (time.monotonic() - start_ts) >= time_limit:
                finished = False
                break
        else:
            finished = True

        text = "".join(accumulated)
        if not finished:
            with self._lock:
                self.context_dict[pid] = {
                    "model_name": model_name,
                    "messages": messages,
                    "accumulated": accumulated,
                    "iterator": iterator,
                    "updated_at": time.time(),
                }
        else:
            self.clear_restoration(pid)

        return text, finished
