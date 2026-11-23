from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseContextManager(ABC):
    def __init__(self):
        self.active = False

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    @abstractmethod
    def save_context(self, *args, **kwargs) -> Any:
        pass

    @abstractmethod
    def load_context(self, pid: int, *args, **kwargs) -> Optional[Any]:
        pass

    @abstractmethod
    def check_restoration(self, pid: int) -> bool:
        pass

    @abstractmethod
    def clear_restoration(self, pid: int) -> None:
        pass
