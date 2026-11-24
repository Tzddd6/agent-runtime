# Project module contract / 项目模块说明
# 职责：管理上下文快照、内容对象与读取预算；文本引用复用不是远端GPU KV Cache零拷贝。
# 主要接口：BaseContextManager。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
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
