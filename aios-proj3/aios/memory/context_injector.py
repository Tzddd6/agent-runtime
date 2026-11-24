# Project module contract / 项目模块说明
# 职责：管理记忆预算、检索与注入；跨Agent/用户访问以显式身份及策略为界。
# 主要接口：ContextInjector。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
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
