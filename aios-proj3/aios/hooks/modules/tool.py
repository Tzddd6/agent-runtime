from aios.hooks.stores._global import (
    global_tool_req_queue,
    global_tool_req_queue_get_message,
    global_tool_req_queue_add_message,
    global_tool_req_queue_is_empty,
)


def useToolRequestQueue():
    return (
        global_tool_req_queue,
        global_tool_req_queue_get_message,
        global_tool_req_queue_add_message,
        global_tool_req_queue_is_empty,
    )


def useToolManager(log_mode: str = "console", autostart_mcp: bool = False):
    from aios.tool.manager import ToolManager
    return ToolManager(log_mode=log_mode, autostart_mcp=autostart_mcp)
