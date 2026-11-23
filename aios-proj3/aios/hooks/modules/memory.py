from typing import Any, Dict, Optional
from aios.hooks.stores._global import (
    global_memory_req_queue,
    global_memory_req_queue_get_message,
    global_memory_req_queue_add_message,
    global_memory_req_queue_is_empty,
)


def useMemoryRequestQueue():
    return (
        global_memory_req_queue,
        global_memory_req_queue_get_message,
        global_memory_req_queue_add_message,
        global_memory_req_queue_is_empty,
    )


def useMemoryManager(
    memory_limit: int = 104857600,
    eviction_k: int = 2,
    log_mode: str = "console",
    provider: Optional[str] = None,
):
    from aios.memory.manager import MemoryManager
    return MemoryManager(
        memory_limit=memory_limit,
        eviction_k=eviction_k,
        log_mode=log_mode,
        provider=provider,
    )
