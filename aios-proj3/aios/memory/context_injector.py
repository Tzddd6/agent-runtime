import copy
from typing import Any, Optional
from cerebrum.llm.apis import LLMQuery


class ContextInjector:
    """
    Automatically retrieves relevant user memories from MemoryManager
    and injects them into LLMQuery prior to LLM Core execution.
    """

    def __init__(self, memory_manager: Any, top_k: int = 3):
        self.memory_manager = memory_manager
        self.top_k = top_k

    def inject_Into_query(
        self,
        agent_name: str,
        query: LLMQuery,
        user_id: Optional[str] = None,
    ) -> LLMQuery:
        effective_uid = user_id or query.user_id
        if not effective_uid:
            return query

        last_user_msg = ""
        for msg in reversed(query.messages):
            if msg.get("role") == "user":
                last_user_msg = str(msg.get("content", ""))
                break

        notes = self.memory_manager.search_notes(
            query_text=last_user_msg,
            user_id=effective_uid,
            agent_id=agent_name,
            k=self.top_k,
        )
        if not notes:
            return query

        cloned = copy.deepcopy(query)
        mem_lines = "\n".join(f"- {n['content']}" for n in notes)
        sys_msg = {
            "role": "system",
            "content": f"[AIOS Memory Context for user '{effective_uid}']:\n{mem_lines}",
        }
        cloned.messages = [sys_msg] + list(cloned.messages)
        return cloned
