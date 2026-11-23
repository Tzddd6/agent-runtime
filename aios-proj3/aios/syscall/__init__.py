from threading import Event, Thread
from typing import Any, Dict, Optional
from cerebrum.utils.communication import Query


class Syscall(Thread):
    """
    Base class for system calls in the AIOS Kernel.
    Tracks lifecycle status, PID/Thread ID, timing metrics, resource tags, and synchronization Event.
    """

    def __init__(self, agent_name: str, query: Query):
        super().__init__(daemon=True)
        self.agent_name = agent_name
        self.query = query
        self.event = Event()

        self.syscall_id: Optional[int] = None
        self.pid: Optional[int] = None
        self.aid: Optional[str] = agent_name
        self.status: str = "created"

        self.response: Optional[Any] = None
        self.time_limit: Optional[float] = None

        self.created_time: Optional[float] = None
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None

        self.source: Optional[str] = agent_name
        self.target: Optional[str] = getattr(query, "query_class", "unknown")
        self.priority: int = 1

        # Write barrier metadata for memory consistency
        self.barrier_seq: Optional[int] = None
        self.barrier_token: Optional[str] = None
        self.user_id: Optional[str] = getattr(query, "user_id", None)

    def set_created_time(self, ts: float) -> None:
        self.created_time = ts

    def get_created_time(self) -> Optional[float]:
        return self.created_time

    def set_start_time(self, ts: float) -> None:
        self.start_time = ts

    def get_start_time(self) -> Optional[float]:
        return self.start_time

    def set_end_time(self, ts: float) -> None:
        self.end_time = ts

    def get_end_time(self) -> Optional[float]:
        return self.end_time

    def set_status(self, status: str) -> None:
        self.status = status

    def get_status(self) -> Optional[str]:
        return self.status

    def set_response(self, response: Any) -> None:
        self.response = response

    def get_response(self) -> Optional[Any]:
        return self.response

    def set_pid(self, pid: Optional[int]) -> None:
        self.pid = pid

    def get_pid(self) -> Optional[int]:
        return self.pid

    def set_priority(self, priority: int) -> None:
        self.priority = priority

    def get_priority(self) -> int:
        return self.priority

    def set_time_limit(self, limit: float) -> None:
        self.time_limit = limit

    def get_time_limit(self) -> Optional[float]:
        return self.time_limit

    def to_snapshot(self) -> Dict[str, Any]:
        wait_time = (
            max(0.0, (self.start_time - self.created_time))
            if (self.start_time and self.created_time)
            else 0.0
        )
        turnaround = (
            max(0.0, (self.end_time - self.created_time))
            if (self.end_time and self.created_time)
            else 0.0
        )
        return {
            "syscall_id": self.syscall_id,
            "pid": self.pid,
            "agent_name": self.agent_name,
            "target": self.target,
            "execution": getattr(self, "execution", None),
            "status": self.status,
            "created_time": self.created_time,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "waiting_time": round(wait_time, 6),
            "turnaround_time": round(turnaround, 6),
        }
