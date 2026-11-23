from typing import Any, Callable, Dict


class ToolInstance:
    def __init__(self, name: str, handler: Callable[[Dict[str, Any]], Any], description: str = ""):
        self.name = name
        self.handler = handler
        self.description = description

    def run(self, params: Dict[str, Any]) -> Any:
        return self.handler(params or {})


class AutoTool:
    """Registry and loader for preloaded and dynamically registered AIOS tools."""
    _REGISTRY: Dict[str, ToolInstance] = {}

    @classmethod
    def register(cls, name: str, handler: Callable[[Dict[str, Any]], Any], description: str = "") -> None:
        cls._REGISTRY[name] = ToolInstance(name=name, handler=handler, description=description)

    @classmethod
    def from_preloaded(cls, tool_org_and_name: str) -> ToolInstance:
        if tool_org_and_name not in cls._REGISTRY:
            raise KeyError(f"Tool '{tool_org_and_name}' is not registered in AutoTool")
        return cls._REGISTRY[tool_org_and_name]

    @classmethod
    def list_tools(cls) -> Dict[str, str]:
        return {name: inst.description for name, inst in cls._REGISTRY.items()}
