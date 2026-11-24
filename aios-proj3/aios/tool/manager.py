# Project module contract / 项目模块说明
# 职责：定义受控工具与固定工程；AST限制不等价于任意代码安全沙箱。
# 主要接口：ToolManager。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
import os
import subprocess
import sys
from threading import Lock
from typing import Any, Dict, List

from cerebrum.interface import AutoTool
from cerebrum.tool.apis import ToolResponse
from aios.config.config_manager import config
from aios.tool.mcp_server import register_builtin_tools


class ToolManager:
    """
    AIOS Kernel Tool Manager.
    Manages preloaded and custom tools via AutoTool, enforces per-tool conflict locking,
    and optionally manages the background MCP Server process.
    """

    def __init__(self, log_mode: str = "console", autostart_mcp: bool = False):
        self.log_mode = log_mode
        self.tool_conflict_map: Dict[str, int] = {}
        self.tool_conflict_map_lock = Lock()
        self.mcp_server_process = None
        register_builtin_tools(AutoTool)
        if autostart_mcp:
            self._start_mcp_server()

    def _start_mcp_server(self) -> None:
        if self.mcp_server_process is None or self.mcp_server_process.poll() is not None:
            mcp_path = config.get_mcp_server_script_path()
            if mcp_path and os.path.exists(mcp_path):
                try:
                    self.mcp_server_process = subprocess.Popen(
                        [sys.executable, mcp_path],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                except Exception:
                    self.mcp_server_process = None

    def _stop_mcp_server(self) -> None:
        if self.mcp_server_process and self.mcp_server_process.poll() is None:
            try:
                self.mcp_server_process.terminate()
                self.mcp_server_process.wait(timeout=3)
            except Exception:
                try:
                    self.mcp_server_process.kill()
                except Exception:
                    pass
            finally:
                self.mcp_server_process = None

    def cleanup(self) -> None:
        self._stop_mcp_server()

    def load_tool_instance(self, tool_org_and_name: str):
        return AutoTool.from_preloaded(tool_org_and_name)

    def address_request(self, syscall: Any) -> ToolResponse:
        tool_calls: List[Dict[str, Any]] = getattr(syscall.query, "tool_calls", None) or []
        if not tool_calls:
            return ToolResponse(
                response_message="There is no tool to call",
                finished=False,
                status_code=400,
            )

        results = []
        try:
            for tool_call in tool_calls:
                tool_name = tool_call["name"]
                tool_params = tool_call.get("parameters", {})
                service = getattr(self, 'process_service', None)
                independent = service is not None and tool_name in service.OPERATIONS
                with self.tool_conflict_map_lock:
                    if not independent and tool_name in self.tool_conflict_map:
                        return ToolResponse(
                            response_message=f"Tool conflict detected for '{tool_name}'",
                            finished=False,
                            status_code=409,
                        )
                    if not independent:
                        self.tool_conflict_map[tool_name] = 1
                try:
                    from aios.tool.bindings import execute, BOUND_TOOLS
                    service = getattr(self, 'process_service', None)
                    if tool_name in BOUND_TOOLS and hasattr(self, 'kernel'):
                        res = execute(self.kernel, syscall, tool_name, tool_params)
                    elif service is not None and tool_name in service.OPERATIONS:
                        res = service.run(syscall.agent_name, tool_name, tool_params,
                                          resources=getattr(syscall.query, 'resources', None),
                                          timeout=getattr(syscall.query, 'execution_timeout', 10),
                                          receipt_observer=lambda value: setattr(syscall, 'execution', value))
                    else:
                        tool = self.load_tool_instance(tool_name)
                        res = tool.run(params=tool_params)
                    results.append({"tool": tool_name, "result": res})
                finally:
                    with self.tool_conflict_map_lock:
                        if not independent:
                            self.tool_conflict_map.pop(tool_name, None)

            output = results[0]["result"] if len(results) == 1 else results
            return ToolResponse(response_message=output, finished=True, status_code=200)
        except Exception as exc:
            return ToolResponse(
                response_message=f"Tool calling error: {exc}",
                finished=True,
                error=str(exc),
                status_code=500,
            )
