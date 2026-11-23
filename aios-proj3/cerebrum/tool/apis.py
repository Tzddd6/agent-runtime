from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from cerebrum.utils.communication import Query, Response


@dataclass
class ToolQuery(Query):
    """Query structure for Tool Manager system calls."""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    resources: Optional[Dict[str, int]] = None
    execution_timeout: float = 10.0
    query_class: str = "tool"


@dataclass
class ToolResponse(Response):
    """Response structure from Tool Manager system calls."""
    response_message: Optional[Any] = None
    finished: bool = True
    error: Optional[str] = None
    status_code: int = 200
