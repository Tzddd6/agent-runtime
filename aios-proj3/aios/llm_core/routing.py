from enum import Enum
import threading
from typing import Any, Dict, List


class RouterStrategy(str, Enum):
    Sequential = "sequential"
    Smart = "smart"


class SequentialRouting:
    """Round-robin sequential model router across configured LLM backends."""

    def __init__(self, models: List[Dict[str, Any]]):
        self.models = list(models)
        self._idx = 0
        self._lock = threading.Lock()

    def select(self, requested_model: str | None = None) -> Dict[str, Any]:
        if not self.models:
            return {"name": requested_model or "mock-aios-core", "backend": "mock"}
        if requested_model:
            for m in self.models:
                if m.get("name") == requested_model:
                    return m
        with self._lock:
            selected = self.models[self._idx % len(self.models)]
            self._idx += 1
            return selected


class SmartRouting:
    """Latency and in-flight load aware model router."""

    def __init__(self, models: List[Dict[str, Any]]):
        self.models = list(models)
        self.in_flight: Dict[str, int] = {m.get("name", f"m_{i}"): 0 for i, m in enumerate(self.models)}
        self._lock = threading.Lock()

    def select(self, requested_model: str | None = None) -> Dict[str, Any]:
        if not self.models:
            return {"name": requested_model or "mock-aios-core", "backend": "mock"}
        if requested_model:
            for m in self.models:
                if m.get("name") == requested_model:
                    return m
        with self._lock:
            best = min(self.models, key=lambda m: self.in_flight.get(m.get("name", ""), 0))
            name = best.get("name", "")
            self.in_flight[name] = self.in_flight.get(name, 0) + 1
            return best

    def release(self, model_name: str) -> None:
        with self._lock:
            if model_name in self.in_flight and self.in_flight[model_name] > 0:
                self.in_flight[model_name] -= 1
