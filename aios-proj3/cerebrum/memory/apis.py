from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional
from cerebrum.utils.communication import Query, Response


@dataclass
class MemoryQuery(Query):
    """Query structure for Memory Manager system calls."""
    operation_type: Literal[
        "add_memory",
        "remove_memory",
        "update_memory",
        "get_memory",
        "retrieve_memory",
        "retrieve_memory_raw",
        "clear_memories",
    ] = "retrieve_memory"
    params: Dict[str, Any] = field(default_factory=dict)
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    query_class: str = "memory"


@dataclass
class MemoryResponse(Response):
    """Response structure from Memory Manager system calls."""
    response_message: Optional[Any] = None
    memory_id: Optional[str] = None
    search_results: Optional[List[Dict[str, Any]]] = None
    finished: bool = True
    error: Optional[str] = None
    status_code: int = 200

    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        data["memory_id"] = self.memory_id
        data["search_results"] = self.search_results
        return data
