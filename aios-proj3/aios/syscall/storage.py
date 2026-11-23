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
