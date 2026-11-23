"""
LiteCUA: Computer as MCP Server for Computer-Use Agent on AIOS (arXiv:2505.18829).
Provides a sandboxed VM / Virtual Desktop Controller and MCP Tool Adapter that maps
high-level agent computer-use intentions (observe accessibility tree, click, type,
hotkey, snapshot/restore) to safe, audited computer operations.
"""
import copy
import threading
import time
from typing import Any, Dict, List, Optional


class VMController:
    """
    Sandboxed Virtual Desktop & Accessibility Tree Controller for LiteCUA.
    Maintains screen geometry, active windows, interactive UI elements (a11y tree),
    cursor coordinates, clipboard, and state snapshots for deterministic rollback.
    """

    def __init__(self, width: int = 1920, height: int = 1080):
        self.width = width
        self.height = height
        self._lock = threading.RLock()
        self._initial_state: Dict[str, Any] = {
            "active_window": "AIOS_Code_Workspace",
            "cursor": {"x": 960, "y": 540},
            "clipboard": "",
            "a11y_tree": [
                {
                    "element_id": "btn_run_tests",
                    "role": "button",
                    "label": "Run Unit Tests",
                    "bbox": [40, 60, 180, 96],
                    "focused": False,
                    "value": "",
                },
                {
                    "element_id": "input_search_bar",
                    "role": "textbox",
                    "label": "Search Files or Symbols",
                    "bbox": [220, 60, 680, 96],
                    "focused": True,
                    "value": "",
                },
                {
                    "element_id": "editor_pane_main",
                    "role": "document",
                    "label": "kernel/launch.py",
                    "bbox": [40, 120, 1600, 980],
                    "focused": False,
                    "value": "# AIOS Kernel Main Entry\n",
                },
            ],
            "action_log": [],
        }
        self._state = copy.deepcopy(self._initial_state)
        self._snapshots: Dict[str, Dict[str, Any]] = {"boot": copy.deepcopy(self._initial_state)}

    def observe(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "resolution": [self.width, self.height],
                "active_window": self._state["active_window"],
                "cursor": dict(self._state["cursor"]),
                "a11y_tree": copy.deepcopy(self._state["a11y_tree"]),
                "action_count": len(self._state["action_log"]),
            }

    def click(
        self,
        element_id: Optional[str] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
    ) -> Dict[str, Any]:
        with self._lock:
            clicked_elem = None
            if element_id:
                for el in self._state["a11y_tree"]:
                    if el["element_id"] == element_id:
                        clicked_elem = el
                        bx1, by1, bx2, by2 = el["bbox"]
                        self._state["cursor"] = {"x": (bx1 + bx2) // 2, "y": (by1 + by2) // 2}
                        el["focused"] = True
                    else:
                        el["focused"] = False
                if clicked_elem is None:
                    raise ValueError(f"Element '{element_id}' not found in accessibility tree")
            elif x is not None and y is not None:
                if not (0 <= int(x) <= self.width and 0 <= int(y) <= self.height):
                    raise ValueError(f"Coordinates ({x}, {y}) outside VM screen bounds ({self.width}x{self.height})")
                self._state["cursor"] = {"x": int(x), "y": int(y)}
                for el in self._state["a11y_tree"]:
                    bx1, by1, bx2, by2 = el["bbox"]
                    if bx1 <= int(x) <= bx2 and by1 <= int(y) <= by2:
                        el["focused"] = True
                        clicked_elem = el
                    else:
                        el["focused"] = False
            else:
                raise ValueError("Click requires either 'element_id' or ('x', 'y') coordinates")

            record = {
                "op": "click",
                "button": button,
                "element_id": clicked_elem["element_id"] if clicked_elem else None,
                "cursor": dict(self._state["cursor"]),
                "timestamp": time.time(),
            }
            self._state["action_log"].append(record)
            return record

    def type_text(self, text: str, element_id: Optional[str] = None, append: bool = True) -> Dict[str, Any]:
        with self._lock:
            target = None
            if element_id:
                target = next((el for el in self._state["a11y_tree"] if el["element_id"] == element_id), None)
            else:
                target = next((el for el in self._state["a11y_tree"] if el.get("focused")), None)
            if target is None:
                raise ValueError("No focused or specified UI element available for typing")

            if append:
                target["value"] = str(target.get("value", "")) + str(text)
            else:
                target["value"] = str(text)

            record = {
                "op": "type",
                "element_id": target["element_id"],
                "value": target["value"],
                "timestamp": time.time(),
            }
            self._state["action_log"].append(record)
            return record

    def hotkey(self, keys: List[str]) -> Dict[str, Any]:
        if not keys or not isinstance(keys, list):
            raise ValueError("Hotkey requires a non-empty list of key names")
        combo = "+".join(str(k).lower() for k in keys)
        with self._lock:
            record = {"op": "hotkey", "combo": combo, "timestamp": time.time()}
            self._state["action_log"].append(record)
            return record

    def save_snapshot(self, name: str) -> Dict[str, Any]:
        with self._lock:
            self._snapshots[name] = copy.deepcopy(self._state)
            return {"snapshot": name, "saved": True}

    def restore_snapshot(self, name: str = "boot") -> Dict[str, Any]:
        with self._lock:
            if name not in self._snapshots:
                raise KeyError(f"VM snapshot '{name}' not found")
            self._state = copy.deepcopy(self._snapshots[name])
            return {"snapshot": name, "restored": True, "active_window": self._state["active_window"]}


_GLOBAL_VM_CONTROLLER = VMController()


def get_vm_controller() -> VMController:
    return _GLOBAL_VM_CONTROLLER


def register_litecua_tools(auto_tool_cls: Any) -> None:
    """Register LiteCUA Computer-Use MCP tools into AIOS AutoTool registry."""
    vm = get_vm_controller()
    auto_tool_cls.register(
        "litecua/observe",
        lambda params: vm.observe(),
        "LiteCUA MCP: Observe virtual computer screen state and accessibility tree",
    )
    auto_tool_cls.register(
        "litecua/click",
        lambda params: vm.click(
            element_id=params.get("element_id"),
            x=params.get("x"),
            y=params.get("y"),
            button=params.get("button", "left"),
        ),
        "LiteCUA MCP: Click an accessibility tree element or screen coordinate",
    )
    auto_tool_cls.register(
        "litecua/type",
        lambda params: vm.type_text(
            text=str(params.get("text", "")),
            element_id=params.get("element_id"),
            append=bool(params.get("append", True)),
        ),
        "LiteCUA MCP: Type text into the focused or specified UI element",
    )
    auto_tool_cls.register(
        "litecua/hotkey",
        lambda params: vm.hotkey(params.get("keys", [])),
        "LiteCUA MCP: Execute a keyboard shortcut combination in the virtual desktop",
    )
    auto_tool_cls.register(
        "litecua/snapshot",
        lambda params: (
            vm.save_snapshot(params.get("name", "checkpoint"))
            if params.get("action", "restore") == "save"
            else vm.restore_snapshot(params.get("name", "boot"))
        ),
        "LiteCUA MCP: Save or restore virtual computer environment state snapshot",
    )
