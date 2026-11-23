import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from cerebrum.llm.apis import LLMQuery, LLMResponse
from cerebrum.memory.apis import MemoryQuery, MemoryResponse
from cerebrum.storage.apis import StorageQuery, StorageResponse
from cerebrum.tool.apis import ToolQuery, ToolResponse

from aios.hooks.stores._global import (
    global_llm_req_queue_add_message,
    global_memory_req_queue_add_message,
    global_storage_req_queue_add_message,
    global_tool_req_queue_add_message,
)
from aios.syscall import Syscall
from aios.syscall.llm import LLMSyscall
from aios.syscall.memory import MemorySyscall
from aios.syscall.storage import StorageSyscall
from aios.syscall.tool import ToolSyscall


class SyscallWrapper:
    """Compatibility wrapper for syscall inspection."""
    pass


class SyscallExecutor:
    """
    Central System Call Dispatcher & Lifecycle Manager of the AIOS Kernel.
    Converts high-level Agent Queries into atomic Syscalls, enforces MemoryWriteBarrier
    ordering, dispatches to the 4 subsystem queues, and supports multi-slice RR execution.
    """

    def __init__(self, max_history: int = 500, *, submit: Optional[Callable[[Syscall], None]] = None):
        self.id = 0
        self.id_lock = threading.Lock()
        self.context_injector = None
        self.conversation_extractor = None
        self.memory_manager = None
        self.submit = submit or self._enqueue_syscall
        self.history_lock = threading.Lock()
        self.history: Deque[Dict[str, Any]] = deque(maxlen=max_history)

    def _resolve_memory_manager(self):
        if self.memory_manager is not None:
            return self.memory_manager
        if self.context_injector is not None:
            return getattr(self.context_injector, "memory_manager", None)
        return None

    def create_syscall(self, agent_name: str, query: Any) -> Syscall:
        for query_type, syscall_type, target in ((LLMQuery, LLMSyscall, "llm"),
                (StorageQuery, StorageSyscall, "storage"), (MemoryQuery, MemorySyscall, "memory"),
                (ToolQuery, ToolSyscall, "tool")):
            if isinstance(query, query_type):
                syscall = syscall_type(agent_name, query)
                syscall.target = target
                syscall.resubmit = self.submit
                return syscall
        raise TypeError(f"Unsupported query type: {type(query)!r}")

    def _enqueue_syscall(self, syscall: Syscall) -> None:
        if isinstance(syscall, LLMSyscall):
            global_llm_req_queue_add_message(syscall)
        elif isinstance(syscall, MemorySyscall):
            global_memory_req_queue_add_message(syscall)
        elif isinstance(syscall, StorageSyscall):
            global_storage_req_queue_add_message(syscall)
        elif isinstance(syscall, ToolSyscall):
            global_tool_req_queue_add_message(syscall)
        else:
            raise TypeError(f"Unknown syscall instance: {type(syscall)!r}")

    def _execute_syscall(self, agent_name: str, query: Any) -> Dict[str, Any]:
        start_times: List[float] = []
        end_times: List[float] = []
        waiting_times: List[float] = []
        turnaround_times: List[float] = []

        with self.id_lock:
            self.id += 1
            syscall_id = self.id

        mem_mgr = self._resolve_memory_manager()
        barrier = getattr(mem_mgr, "barrier", None) if mem_mgr is not None else None
        user_id = getattr(query, "user_id", None) or "default"

        barrier_token: Optional[str] = None
        barrier_seq: Optional[int] = None

        if barrier is not None:
            if isinstance(query, MemoryQuery) and query.operation_type in (
                "add_memory",
                "update_memory",
                "remove_memory",
                "clear_memories",
            ):
                barrier_token, barrier_seq = barrier.register_write(user_id)
            else:
                barrier_seq = barrier.snapshot_seq(user_id)

        try:
            syscall = self.create_syscall(agent_name, query)
            syscall.syscall_id = syscall_id
            syscall.set_pid(syscall_id)
            syscall.user_id = user_id
            syscall.barrier_seq = barrier_seq
            syscall.barrier_token = barrier_token
            while True:
                syscall.set_status("active")
                syscall.set_response(None)
                syscall.start_time = None
                syscall.end_time = None

                created_at = time.time()
                syscall.set_created_time(created_at)
                syscall.event.clear()

                self.submit(syscall)
                syscall.event.wait()

                start_time = syscall.get_start_time() or created_at
                end_time = syscall.get_end_time() or time.time()
                waiting_time = max(0.0, start_time - created_at)
                turnaround_time = max(0.0, end_time - created_at)

                start_times.append(start_time)
                end_times.append(end_time)
                waiting_times.append(waiting_time)
                turnaround_times.append(turnaround_time)

                with self.history_lock:
                    self.history.append(syscall.to_snapshot())

                response = syscall.get_response()
                if response is None or getattr(response, "finished", True):
                    break
        finally:
            if barrier is not None and barrier_token is not None:
                barrier.complete_write(user_id, barrier_token)

        return {
            "syscall_id": syscall_id,
            "response": response,
            "start_times": start_times,
            "end_times": end_times,
            "waiting_times": waiting_times,
            "turnaround_times": turnaround_times,
        }

    def execute_request(self, agent_name: str, query: Any) -> Dict[str, Any]:
        if isinstance(query, LLMQuery) and self.context_injector is not None:
            user_id = getattr(query, "user_id", None)
            mem_mgr = self._resolve_memory_manager()
            if user_id and mem_mgr is not None and getattr(mem_mgr, "barrier", None) is not None:
                snap = mem_mgr.barrier.snapshot_seq(user_id)
                mem_mgr.barrier.wait_for_seq(user_id, snap)
            query = self.context_injector.inject_Into_query(agent_name, query, user_id=user_id)

        return self._execute_syscall(agent_name, query)

    def get_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.history_lock:
            return list(self.history)[-limit:] if limit > 0 else []


_GLOBAL_SYSCALL_EXECUTOR = SyscallExecutor()


def useSysCall() -> Tuple[Any, Any, SyscallExecutor]:
    return (
        _GLOBAL_SYSCALL_EXECUTOR.execute_request,
        SyscallWrapper,
        _GLOBAL_SYSCALL_EXECUTOR,
    )
