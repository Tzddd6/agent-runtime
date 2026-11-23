from contextlib import contextmanager
from typing import Any, Callable, Optional, Tuple
from aios.hooks.stores._global import (
    global_llm_req_queue_get_message,
    global_memory_req_queue_get_message,
    global_storage_req_queue_get_message,
    global_tool_req_queue_get_message,
)
from aios.scheduler.fifo_scheduler import FIFOScheduler
from aios.scheduler.rr_scheduler import RRScheduler


def fifo_scheduler_nonblock(
    llm: Any,
    memory_manager: Any,
    storage_manager: Any,
    tool_manager: Any,
    log_mode: str = "console",
    get_llm_syscall: Optional[Callable] = None,
    get_memory_syscall: Optional[Callable] = None,
    get_storage_syscall: Optional[Callable] = None,
    get_tool_syscall: Optional[Callable] = None,
    batch_interval: float = 0.05,
) -> FIFOScheduler:
    return FIFOScheduler(
        llm=llm,
        memory_manager=memory_manager,
        storage_manager=storage_manager,
        tool_manager=tool_manager,
        log_mode=log_mode,
        get_llm_syscall=get_llm_syscall or global_llm_req_queue_get_message,
        get_memory_syscall=get_memory_syscall or global_memory_req_queue_get_message,
        get_storage_syscall=get_storage_syscall or global_storage_req_queue_get_message,
        get_tool_syscall=get_tool_syscall or global_tool_req_queue_get_message,
        batch_interval=batch_interval,
    )


def rr_scheduler_nonblock(
    llm: Any,
    memory_manager: Any,
    storage_manager: Any,
    tool_manager: Any,
    log_mode: str = "console",
    get_llm_syscall: Optional[Callable] = None,
    get_memory_syscall: Optional[Callable] = None,
    get_storage_syscall: Optional[Callable] = None,
    get_tool_syscall: Optional[Callable] = None,
    time_slice: float = 0.2,
) -> RRScheduler:
    return RRScheduler(
        llm=llm,
        memory_manager=memory_manager,
        storage_manager=storage_manager,
        tool_manager=tool_manager,
        log_mode=log_mode,
        get_llm_syscall=get_llm_syscall or global_llm_req_queue_get_message,
        get_memory_syscall=get_memory_syscall or global_memory_req_queue_get_message,
        get_storage_syscall=get_storage_syscall or global_storage_req_queue_get_message,
        get_tool_syscall=get_tool_syscall or global_tool_req_queue_get_message,
        time_slice=time_slice,
    )


@contextmanager
def fifo_scheduler(**kwargs):
    scheduler = fifo_scheduler_nonblock(**kwargs)
    scheduler.start()
    try:
        yield scheduler
    finally:
        scheduler.stop()


@contextmanager
def rr_scheduler(**kwargs):
    scheduler = rr_scheduler_nonblock(**kwargs)
    scheduler.start()
    try:
        yield scheduler
    finally:
        scheduler.stop()
