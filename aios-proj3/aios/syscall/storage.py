# Project module contract / 项目模块说明
# 职责：封装类型化调用、分派与测量；结果和错误必须明确返回。
# 主要接口：StorageSyscall。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
from aios.syscall import Syscall


class StorageSyscall(Syscall):
    pass


storage_syscalls = [
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "create a file",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_dir",
            "description": "create a directory",
            "parameters": {
                "type": "object",
                "properties": {"dir_path": {"type": "string"}},
                "required": ["dir_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "write content into a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "read content from a file",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "delete a file",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve",
            "description": "retrieve files by semantic query or keywords",
            "parameters": {
                "type": "object",
                "properties": {
                    "query_text": {"type": "string"},
                    "k": {"type": "integer", "default": 3},
                    "keywords": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rollback",
            "description": "rollback a file to a previous version",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "n": {"type": "integer", "default": 1},
                },
                "required": ["file_path"],
            },
        },
    },
]
