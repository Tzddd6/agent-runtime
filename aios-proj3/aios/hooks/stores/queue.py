from queue import Queue
from typing import Any, Dict

REQUEST_QUEUE: Dict[str, Queue] = {}


def get_queue(name: str) -> Queue:
    if name not in REQUEST_QUEUE:
        REQUEST_QUEUE[name] = Queue()
    return REQUEST_QUEUE[name]


def add_message(q: Queue, message: Any) -> None:
    q.put(message)


def get_message(q: Queue, timeout: float = 0.05) -> Any:
    if timeout <= 0:
        return q.get_nowait()
    return q.get(block=True, timeout=timeout)


def is_empty(q: Queue) -> bool:
    return q.empty()
