from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Query:
    """Base query class for all Cerebrum -> AIOS Kernel requests."""
    query_class: str = "base"

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Response:
    """Base response class returned by AIOS Kernel to Cerebrum SDK."""
    response_message: Optional[Any] = None
    finished: bool = True
    error: Optional[str] = None
    status_code: int = 200
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "response_message": self.response_message,
            "finished": self.finished,
            "error": self.error,
            "status_code": self.status_code,
            "metadata": dict(self.metadata),
        }
