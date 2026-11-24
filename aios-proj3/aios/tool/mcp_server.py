# Project module contract / 项目模块说明
# 职责：定义受控工具与固定工程；AST限制不等价于任意代码安全沙箱。
# 主要接口：safe_eval_math, register_builtin_tools。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
"""
Lightweight MCP-compatible Tool Server for AIOS ToolManager.
Can run either embedded in-process or as a standalone stdio/subprocess server.
"""
import ast
from datetime import datetime, timezone
import json
import operator
import platform
import sys
from typing import Any, Dict


_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def safe_eval_math(expr: str) -> float | int:
    def _eval(node: ast.AST) -> float | int:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
            return _SAFE_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
            return _SAFE_OPS[type(node.op)](_eval(node.operand))
        raise ValueError(f"Unsupported arithmetic syntax: {ast.dump(node)}")

    tree = ast.parse(str(expr).strip(), mode="eval")
    return _eval(tree)


def register_builtin_tools(auto_tool_cls: Any) -> None:
    auto_tool_cls.register(
        "math/calculate",
        lambda params: {"result": safe_eval_math(params.get("expression", "0"))},
        "Evaluate a safe arithmetic expression",
    )
    auto_tool_cls.register(
        "math.calculate",
        lambda params: {"result": safe_eval_math(params.get("expression", "0"))},
        "Evaluate a safe arithmetic expression (alias)",
    )
    auto_tool_cls.register(
        "clock/now",
        lambda params: {"utc_iso": datetime.now(timezone.utc).isoformat()},
        "Get current UTC timestamp in ISO-8601 format",
    )
    auto_tool_cls.register(
        "text/stats",
        lambda params: {
            "chars": len(str(params.get("text", ""))),
            "words": len(str(params.get("text", "")).split()),
            "lines": len(str(params.get("text", "")).splitlines()),
        },
        "Compute character, word, and line counts for input text",
    )
    auto_tool_cls.register(
        "system/info",
        lambda params: {
            "os": platform.system(),
            "python_version": platform.python_version(),
            "kernel": "AIOS-Formal-Kernel",
        },
        "Return non-sensitive AIOS runtime environment metadata",
    )
    from aios.tool.virtual_env.desktop_env import register_litecua_tools
    from aios.tool.catalog import register_extended_catalog_tools
    register_litecua_tools(auto_tool_cls)
    register_extended_catalog_tools(auto_tool_cls)


if __name__ == "__main__":
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if req.get("method") == "ping":
                print(json.dumps({"status": "ok", "server": "aios-mcp"}), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), flush=True)
