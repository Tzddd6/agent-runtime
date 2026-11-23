from typing import Any, Dict, List
from aios.hooks.stores._global import (
    global_llm_req_queue,
    global_llm_req_queue_get_message,
    global_llm_req_queue_add_message,
    global_llm_req_queue_is_empty,
)


def useLLMRequestQueue():
    return (
        global_llm_req_queue,
        global_llm_req_queue_get_message,
        global_llm_req_queue_add_message,
        global_llm_req_queue_is_empty,
    )


def useCore(
    llm_configs: List[Dict[str, Any]],
    log_mode: str = "console",
    use_context_manager: bool = True,
):
    from aios.llm_core.adapter import LLMAdapter
    return LLMAdapter(
        llm_configs=llm_configs,
        log_mode=log_mode,
        use_context_manager=use_context_manager,
    )
