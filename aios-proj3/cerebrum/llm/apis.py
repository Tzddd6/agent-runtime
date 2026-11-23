from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional
from cerebrum.utils.communication import Query, Response


@dataclass
class LLMQuery(Query):
    """Query structure for LLM Core system calls."""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    tools: Optional[List[Dict[str, Any]]] = None
    action_type: Literal[
        "chat",
        "tool_use",
        "operate_file",
        "create_memory",
        "retrieve_memory",
    ] = "chat"
    message_return_type: str = "text"
    model: Optional[str] = None
    temperature: float = 0.7
    max_new_tokens: int = 1024
    seed: Optional[int] = None
    tool_choice: Optional[Any] = None
    response_format: Optional[Dict[str, Any]] = None
    stream: bool = False
    user_id: Optional[str] = None
    query_class: str = "llm"


@dataclass
class LLMResponse(Response):
    """Response structure from LLM Core system calls."""
    response_message: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    finished: bool = True
    error: Optional[str] = None
    status_code: int = 200
    usage: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = super().to_dict()
        data["tool_calls"] = self.tool_calls
        data["usage"] = dict(self.usage)
        return data
