import json
import urllib.request
from typing import Any, Callable, Dict, Optional, Union

from cerebrum.utils.communication import Query
from cerebrum.llm.apis import LLMQuery, LLMResponse
from cerebrum.memory.apis import MemoryQuery, MemoryResponse
from cerebrum.storage.apis import StorageQuery, StorageResponse
from cerebrum.tool.apis import ToolQuery, ToolResponse


class CerebrumClient:
    """
    AIOS SDK Client (Cerebrum) supporting both:
    - Mode 1 (Local Kernel Mode): direct in-process syscall execution via kernel executor
    - Mode 2 (Remote Kernel Mode): HTTP POST to `/query` on a running AIOS Kernel daemon
    """

    def __init__(
        self,
        kernel_url: Optional[str] = None,
        local_executor: Optional[Callable[[str, Query], Dict[str, Any]]] = None,
        rpc_handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        default_user_id: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.kernel_url = kernel_url.rstrip("/") if kernel_url else None
        self.local_executor = local_executor
        self.rpc_handler = rpc_handler
        self.default_user_id = default_user_id
        self.timeout = timeout

    def query(
        self,
        agent_name: str,
        query_obj: Union[LLMQuery, MemoryQuery, StorageQuery, ToolQuery],
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Submit a typed Query to the AIOS Kernel (Local or Remote)."""
        effective_user_id = user_id or getattr(query_obj, "user_id", None) or self.default_user_id
        if effective_user_id and hasattr(query_obj, "user_id") and not getattr(query_obj, "user_id", None):
            setattr(query_obj, "user_id", effective_user_id)

        if self.local_executor is not None:
            return self.local_executor(agent_name, query_obj)

        payload = {
            "agent_name": agent_name,
            "query_type": getattr(query_obj, "query_class", "llm"),
            "query_data": query_obj.to_dict(),
            "user_id": effective_user_id,
        }

        if self.rpc_handler is not None:
            return self.rpc_handler(payload)

        if not self.kernel_url:
            from aios.syscall.syscall import useSysCall
            execute_request, _, _ = useSysCall()
            return execute_request(agent_name, query_obj)

        req = urllib.request.Request(
            f"{self.kernel_url}/query",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def llm_chat(
        self,
        agent_name: str,
        messages: list,
        model: Optional[str] = None,
        tools: Optional[list] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.query(
            agent_name,
            LLMQuery(messages=messages, model=model, tools=tools, user_id=user_id),
            user_id=user_id,
        )

    def call_tool(
        self,
        agent_name: str,
        tool_name: str,
        parameters: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.query(
            agent_name,
            ToolQuery(tool_calls=[{"name": tool_name, "parameters": parameters}]),
        )

    def memory_op(
        self,
        agent_name: str,
        operation_type: str,
        params: Dict[str, Any],
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.query(
            agent_name,
            MemoryQuery(
                operation_type=operation_type,
                params=params,
                user_id=user_id,
                agent_id=agent_name,
            ),
            user_id=user_id,
        )

    def storage_op(
        self,
        agent_name: str,
        operation_type: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        return self.query(
            agent_name,
            StorageQuery(operation_type=operation_type, params=params),
        )
