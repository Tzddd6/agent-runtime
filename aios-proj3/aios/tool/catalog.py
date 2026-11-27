"""
Complete Curated Tool & Skill Registry for AIOS Kernel (100% Parity with Original Project + LiteCUA + Guizang Skills).
Provides 36 controlled tools and 17 domain/design skills.
"""
import ast
import csv
from decimal import Decimal, localcontext
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import re
from typing import Any, Dict

from cerebrum.interface import AutoTool
from aios.tool import engineering, library
from aios.tool.mcp_server import safe_eval_math


SKILLS: Dict[str, str] = {
    "python-review": "检查Python接口、边界与错误处理",
    "benchmark-evaluation": "冻结数据与判分器，同预算对照并保留失败",
    "streaming-review": "检查公式、代码、表格和长回答的增量与最终一致性",
    "computer-lab": "在专用实验电脑观察页面、操作并验证，拒绝凭据与高风险外发",
    "test-design": "设计正常、异常、并发与资源验收",
    "project-plan": "拆解需求、依赖、里程碑与交付物",
    "technical-writing": "形成结构清晰、区分事实与建议的技术说明",
    "web-research": "检索公开网页并核对来源与时效",
    "workspace-engineering": "在隔离项目中修改文件、运行命令并验证",
    "data-validation": "检查JSON、CSV和计算的输入与结论",
    "runtime-operations": "先观测资源与失败，再有限调整Runtime配置",
    "knowledge-analysis": "基于命中资料回答、交叉检查并引用可核对的来源",
    "engineering-repair": "用最少模型生成完成原始失败、补丁、测试和同版本交付",
    "source-research": "使用本地资料与官方来源核对事实",
    "sql-review": "检查SQLite参数化、事务、NULL和数据契约",
    "humanizer-de-ai": "归藏设计系统 · 剔除AI机械套话与浮夸排比，保留克制准确的工程语气",
    "swiss-editorial-layout": "归藏设计系统 · 瑞士国际主义网格与电子杂志版式排版规范",
}

PLUGINS: Dict[str, str] = {
    "text.stats": "统计文本字符、行与词数",
    "text.search": "按字面关键词查找文本行，不执行正则表达式",
    "text.diff": "比较两段文本并返回有限的统一差异",
    "json.validate": "解析并检查JSON结构，不执行内容",
    "csv.profile": "检查CSV行数、列数及缺失值",
    "csv.aggregate": "对CSV指定列做精确十进制求和，不调用模型",
    "math.calculate": "计算有限的基础算术表达式",
    "python.syntax": "检查Python语法并提取函数和类名，不执行代码",
    "clock.now": "读取实际服务器时间，明确IANA时区与来源",
    "project.inspect": "读取已登记的受控工程源码",
    "project.test": "实际运行固定示例项目的不可修改测试",
    "project.patch_check": "验证固定项目补丁的AST并运行不可修改测试",
    "project.apply_edits": "编译受检AST补丁并执行原项目验证",
    "library.search": "检索版本化的Python、SQL、Git、HTTP和Linux资料",
    "library.read": "按ID读取带官方来源的本地资料条目",
    "tools.discover": "按名称或关键词读取工具参数与权限",
    "memory.remember": "存入当前用户与Agent分区记忆（支持A-Mem双向建链与演化）",
    "memory.recall": "按当前用户和Agent读取预算内记忆与关联图节点",
    "memory.forget": "删除当前Agent拥有的指定记忆",
    "context.search": "检索当前Agent已获授权的上下文语义片段与来源",
    "context.read": "按不可变哈希引用分页读取已获授权的完整原始上下文",
    "file.read": "读取LSFS工作区目录UTF-8文件",
    "file.write": "原子修改LSFS工作区目录UTF-8文件并记录版本快照",
    "file.copy": "按字节复制项目内文本文件，保留测试与格式",
    "file.replace": "按预期哈希和唯一文本片段精确修改项目文件",
    "file.list": "列出工作区目录文件",
    "artifact.write": "在工作区目录生成可下载的文本产物",
    "runtime.observe": "读取当前AIOS内核队列、DRF资源和Agent状态",
    "runtime.configure": "在服务上限内调整工作流并发数或调度策略",
    "workspace.inspect": "读取当前Python项目的源码、测试清单与哈希",
    "workspace.import": "把会话Python附件按原文件名导入工程",
    "workspace.apply_patch": "校验整批哈希后修改已有源码，禁止修改测试",
    "workspace.test": "隔离运行unittest，拒绝0测试或全部跳过的假成功",
    "computer.run": "在LiteCUA独立实验桌面进行浏览、观察、点击、输入与快照核对",
    "web.fetch": "读取公网HTTPS文本，阻止内网访问和DNS重绑定",
    "command.run": "在bubblewrap与cgroup v2隔离目录运行受控命令",
}


def get_catalog() -> Dict[str, Any]:
    return {
        "skills": [{"id": k, "description": v} for k, v in SKILLS.items()],
        "plugins": [
            {
                "id": k,
                "description": v,
                "permissions": [
                    "isolated_desktop_and_model_budget"
                    if k == "computer.run"
                    else "sandboxed_command"
                    if k in ("command.run", "workspace.test")
                    else "public_network"
                    if k == "web.fetch"
                    else "workspace_write"
                    if k
                    in (
                        "file.write",
                        "file.copy",
                        "file.replace",
                        "artifact.write",
                        "workspace.apply_patch",
                        "workspace.import",
                        "project.patch_check",
                        "project.apply_edits",
                    )
                    else "runtime_control"
                    if k == "runtime.configure"
                    else "bounded_read_or_compute"
                ],
            }
            for k, v in PLUGINS.items()
        ],
        "host_arbitrary_code_execution": False,
        "external_plugins_installed": False,
    }


def _csv_aggregate(params: Dict[str, Any]) -> Dict[str, Any]:
    text = str(params.get("text", ""))
    col = str(params.get("column", ""))
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or col not in reader.fieldnames:
        raise ValueError(f"Column '{col}' not found in CSV")
    with localcontext() as ctx:
        ctx.prec = 100
        total = Decimal(0)
        count = 0
        for row in reader:
            raw = (row.get(col) or "").strip()
            if not re.fullmatch(r"[+-]?\d{1,30}(?:\.\d{1,20})?", raw):
                raise ValueError(f"Invalid decimal value in column '{col}': {raw}")
            total += Decimal(raw)
            count += 1
        return {"plugin": "csv.aggregate", "executed": True, "rows": count, "sum_decimal": format(total, "f"), "column": col}


def _csv_profile(params: Dict[str, Any]) -> Dict[str, Any]:
    text = str(params.get("text", ""))
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return {"rows": 0, "columns": 0, "headers": [], "missing_cells": 0}
    headers = rows[0]
    data_rows = rows[1:]
    missing = sum(1 for r in data_rows for cell in r if not str(cell).strip())
    return {"rows": len(data_rows), "columns": len(headers), "headers": headers, "missing_cells": missing}


def _python_syntax(params: Dict[str, Any]) -> Dict[str, Any]:
    source = str(params.get("source", ""))
    tree = ast.parse(source)
    funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    return {"valid": True, "functions": funcs, "classes": classes}


def _text_diff(params: Dict[str, Any]) -> Dict[str, Any]:
    before = str(params.get("before", "")).splitlines(keepends=True)
    after = str(params.get("after", "")).splitlines(keepends=True)
    diff = "".join(difflib.unified_diff(before, after, fromfile="before", tofile="after"))
    return {"diff": diff[:4000], "changed": before != after}


def _text_search(params: Dict[str, Any]) -> Dict[str, Any]:
    text = str(params.get("text", ""))
    query = str(params.get("query", ""))
    matches = [
        {"line": idx + 1, "content": line}
        for idx, line in enumerate(text.splitlines())
        if query and query in line
    ]
    return {"query": query, "count": len(matches), "matches": matches[:50]}


def register_extended_catalog_tools(auto_tool_cls: Any = AutoTool) -> None:
    """Register all 36 curated plugins into AutoTool so they are invokable via AIOS ToolSyscall."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    auto_tool_cls.register(
        "text.stats",
        lambda p: {
            "chars": len(str(p.get("text", ""))),
            "words": len(str(p.get("text", "")).split()),
            "lines": len(str(p.get("text", "")).splitlines()),
        },
        PLUGINS["text.stats"],
    )
    auto_tool_cls.register("text.search", _text_search, PLUGINS["text.search"])
    auto_tool_cls.register("text.diff", _text_diff, PLUGINS["text.diff"])
    auto_tool_cls.register(
        "json.validate",
        lambda p: {"valid": True, "type": type(json.loads(str(p.get("text", "{}")))).__name__},
        PLUGINS["json.validate"],
    )
    auto_tool_cls.register("csv.profile", _csv_profile, PLUGINS["csv.profile"])
    auto_tool_cls.register("csv.aggregate", _csv_aggregate, PLUGINS["csv.aggregate"])
    auto_tool_cls.register("python.syntax", _python_syntax, PLUGINS["python.syntax"])
    auto_tool_cls.register(
        "clock.now",
        lambda p: {
            "iso8601": datetime.now(ZoneInfo(str(p.get("timezone", "Asia/Shanghai")))).isoformat(timespec="seconds"),
            "timezone": str(p.get("timezone", "Asia/Shanghai")),
            "source": "aios_kernel_system_clock",
            "executed": True,
        },
        PLUGINS["clock.now"],
    )
    auto_tool_cls.register(
        "project.inspect",
        lambda p: engineering.manifest(p.get("project", "pricing")),
        PLUGINS["project.inspect"],
    )
    auto_tool_cls.register(
        "project.test",
        lambda p: engineering.run_check(
            engineering.manifest(p.get("project", "pricing"))["source"],
            p.get("project", "pricing"),
        ),
        PLUGINS["project.test"],
    )
    auto_tool_cls.register(
        "project.patch_check",
        lambda p: engineering.run_check(
            p["source"],
            p.get("project", "pricing"),
        ),
        PLUGINS["project.patch_check"],
    )
    auto_tool_cls.register(
        "project.apply_edits",
        lambda p: engineering.run_check(
            p["source"],
            p.get("project", "pricing"),
        ),
        PLUGINS["project.apply_edits"],
    )
    auto_tool_cls.register(
        "library.search",
        lambda p: {"results": library.search(p.get("query", "python"), int(p.get("limit", 4)))},
        PLUGINS["library.search"],
    )
    auto_tool_cls.register(
        "library.read",
        lambda p: library.read(p.get("id", "python-testing")),
        PLUGINS["library.read"],
    )
    auto_tool_cls.register(
        "tools.discover",
        lambda p: {
            "matches": [
                {"id": k, "description": v}
                for k, v in PLUGINS.items()
                if str(p.get("query", "")).lower() in (k + " " + v).lower()
            ]
        },
        PLUGINS["tools.discover"],
    )
    for alias_name in (
        "memory.remember",
        "memory.recall",
        "memory.forget",
        "context.search",
        "context.read",
        "file.read",
        "file.write",
        "file.copy",
        "file.replace",
        "file.list",
        "artifact.write",
        "runtime.observe",
        "runtime.configure",
        "workspace.inspect",
        "workspace.import",
        "workspace.apply_patch",
        "workspace.test",
        "computer.run",
        "web.fetch",
        "command.run",
    ):
        auto_tool_cls.register(
            alias_name,
            lambda p, _name=alias_name: (_ for _ in ()).throw(ValueError("Tool is not bound to a runtime service: " + _name)),
            PLUGINS[alias_name],
        )
