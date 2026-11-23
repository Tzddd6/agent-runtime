from typing import Any, Dict
import threading

AGENT_PROCESSES: Dict[int, Any] = {}
_LOCK = threading.Lock()


def add_process(pid: int, proc: Any) -> None:
    with _LOCK:
        AGENT_PROCESSES[pid] = proc


def remove_process(pid: int) -> None:
    with _LOCK:
        AGENT_PROCESSES.pop(pid, None)


def list_processes() -> Dict[int, Any]:
    with _LOCK:
        return dict(AGENT_PROCESSES)
