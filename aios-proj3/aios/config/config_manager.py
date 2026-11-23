import os
from typing import Any, Dict, Optional

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None


DEFAULT_CONFIG: Dict[str, Any] = {
    "api_keys": {
        "openai": "",
        "deepseek": "",
        "gemini": "",
        "anthropic": "",
    },
    "llms": {
        "default_model": "mock-aios-core",
        "routing_strategy": "sequential",
        "models": [
            {"name": "mock-aios-core", "backend": "mock", "max_new_tokens": 1024},
            {"name": "deepseek-chat", "backend": "deepseek", "hostname": "https://api.deepseek.com/v1"},
        ],
    },
    "memory": {
        "provider": "in-house",
        "memory_limit": 104857600,
        "eviction_k": 2,
        "write_barrier": {
            "enabled": True,
            "timeout_seconds": 5.0,
        },
    },
    "storage": {
        "root_dir": "root_storage",
        "filesystem_type": "lsfs",
        "use_vector_db": False,
    },
    "scheduler": {
        "policy": "fifo",
        "batch_interval": 0.05,
        "time_slice": 0.2,
        "max_workers": 32,
        "drf_enabled": True,
        "cpu_milli_budget": 4000,
        "memory_bytes_budget": 1073741824,
        "pids_budget": 128,
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8000,
    },
}


class ConfigManager:
    """Singleton configuration manager for the AIOS Kernel."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "config"):
            self.config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
            self.config: Dict[str, Any] = {}
            self.load_config()

    def load_config(self) -> None:
        import copy
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        if os.path.exists(self.config_path) and yaml is not None:
            with open(self.config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
                if isinstance(loaded, dict):
                    self.config.update(loaded)

    def save_config(self) -> None:
        if yaml is not None:
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(self.config, f)

    def refresh(self) -> None:
        self.load_config()

    def get_api_key(self, provider: str) -> Optional[str]:
        env_map = {
            "openai": "OPENAI_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }
        key = (self.config.get("api_keys") or {}).get(provider)
        if key:
            return key
        env_var = env_map.get(provider.lower(), f"{provider.upper()}_API_KEY")
        return os.environ.get(env_var)

    def update_api_key(self, provider: str, api_key: str) -> None:
        self.config.setdefault("api_keys", {})[provider] = api_key

    def get_llms_config(self) -> Dict[str, Any]:
        return self.config.get("llms", {})

    def get_memory_config(self) -> Dict[str, Any]:
        return self.config.get("memory", {})

    def get_storage_config(self) -> Dict[str, Any]:
        return self.config.get("storage", {})

    def get_scheduler_config(self) -> Dict[str, Any]:
        return self.config.get("scheduler", {})

    def get_mcp_server_script_path(self) -> str:
        return os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "tool", "mcp_server.py")
        )


config = ConfigManager()
