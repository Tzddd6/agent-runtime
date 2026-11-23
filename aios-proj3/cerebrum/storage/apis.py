from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional
from cerebrum.utils.communication import Query, Response


@dataclass
class StorageQuery(Query):
    """Query structure for Storage Manager (LSFS) system calls."""
    operation_type: Literal[
        "create_file",
        "create_dir",
        "write",
        "read",
        "delete_file",
        "delete_dir",
        "retrieve",
        "rollback",
        "list",
        "share",
    ] = "read"
    params: Dict[str, Any] = field(default_factory=dict)
    messages: Optional[list] = None
    query_class: str = "storage"


@dataclass
class StorageResponse(Response):
    """Response structure from Storage Manager system calls."""
    response_message: Optional[str] = None
    finished: bool = True
    error: Optional[str] = None
    status_code: int = 200
