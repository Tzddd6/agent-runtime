# Project module contract / 项目模块说明
# 职责：封装类型化调用、分派与测量；结果和错误必须明确返回。
# 主要接口：ToolSyscall。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
from aios.syscall import Syscall


class ToolSyscall(Syscall):
    pass
