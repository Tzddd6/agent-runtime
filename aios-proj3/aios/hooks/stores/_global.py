from queue import Queue
from typing import Any, Callable, Tuple
from aios.hooks.stores import queue as QueueStore


def _create_queue_hook(queue_name: str) -> Tuple[Queue, Callable[[], Any], Callable[[Any], None], Callable[[], bool]]:
    q = QueueStore.get_queue(queue_name)

    def get_msg() -> Any:
        return QueueStore.get_message(q)

    def add_msg(message: Any) -> None:
        QueueStore.add_message(q, message)

    def check_empty() -> bool:
        return QueueStore.is_empty(q)

    return q, get_msg, add_msg, check_empty


(
    global_llm_req_queue,
    global_llm_req_queue_get_message,
    global_llm_req_queue_add_message,
    global_llm_req_queue_is_empty,
) = _create_queue_hook("llm")

(
    global_memory_req_queue,
    global_memory_req_queue_get_message,
    global_memory_req_queue_add_message,
    global_memory_req_queue_is_empty,
) = _create_queue_hook("memory")

(
    global_storage_req_queue,
    global_storage_req_queue_get_message,
    global_storage_req_queue_add_message,
    global_storage_req_queue_is_empty,
) = _create_queue_hook("storage")

(
    global_tool_req_queue,
    global_tool_req_queue_get_message,
    global_tool_req_queue_add_message,
    global_tool_req_queue_is_empty,
) = _create_queue_hook("tool")
