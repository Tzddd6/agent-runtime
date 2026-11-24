# Project module contract / 项目模块说明
# 职责：管理受控Worker、资源限制、遥测与回收；严格隔离要求有效Linux委派子树。
# 主要接口：CgroupV2Manager。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
import os
import platform
import re
from typing import Any, Dict, Optional


class CgroupV2Manager:
    """
    OS-level Linux cgroup v2 Hierarchy Controller for AIOS Kernel.
    Implements dual-layer isolation:
        <cgroup_root>/agent_<name>/syscall_<id>/
    Enforces cpu.max, memory.max, memory.swap.max=0, pids.max, and cgroup.kill cleanup.
    Falls back cleanly to `admission_only` telemetry on non-Linux hosts or when
    unconfigured during local development.
    """

    def __init__(self, cgroup_root: Optional[str] = None, strict: bool = False):
        self.cgroup_root = cgroup_root
        self.strict = strict
        self.is_linux = platform.system().lower() == "linux"
        self.enabled = bool(self.is_linux and self.cgroup_root and os.path.exists(self.cgroup_root))
        if self.strict and not self.enabled:
            raise RuntimeError(
                f"Strict cgroup v2 mode requested, but delegated cgroup root '{self.cgroup_root}' is unavailable"
            )

    @staticmethod
    def _sanitize(name: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]", "_", name or "default")[:64]

    def create_syscall_slice(
        self,
        agent_name: str,
        syscall_id: int,
        cpu_milli: int = 1000,
        memory_bytes: int = 256 * 1024 * 1024,
        pids_max: int = 32,
    ) -> Dict[str, Any]:
        if not self.enabled or not self.cgroup_root:
            return {
                "mode": "admission_only",
                "agent": agent_name,
                "syscall_id": syscall_id,
                "limits": {
                    "cpu_milli": cpu_milli,
                    "memory_bytes": memory_bytes,
                    "pids_max": pids_max,
                },
            }

        agent_dir = os.path.join(self.cgroup_root, f"agent_{self._sanitize(agent_name)}")
        slice_dir = os.path.join(agent_dir, f"syscall_{syscall_id}")
        os.makedirs(agent_dir, exist_ok=True)
        self._write_control(os.path.join(agent_dir, "cgroup.subtree_control"), "+cpu +memory +pids\n")
        os.makedirs(slice_dir, exist_ok=True)

        period = 100_000
        quota = max(1000, int(period * (cpu_milli / 1000.0)))
        try:
            self._write_control(os.path.join(slice_dir, "cpu.max"), f"{quota} {period}\n")
            self._write_control(os.path.join(slice_dir, "memory.max"), f"{int(memory_bytes)}\n")
            self._write_control(os.path.join(slice_dir, "memory.swap.max"), "0\n")
            self._write_control(os.path.join(slice_dir, "pids.max"), f"{int(pids_max)}\n")
        except OSError:
            try:
                os.rmdir(slice_dir)
            except OSError:
                pass
            raise

        return {
            "mode": "cgroup_v2_enforced",
            "path": slice_dir,
            "agent": agent_name,
            "syscall_id": syscall_id,
            "limits": {
                "cpu_milli": cpu_milli,
                "memory_bytes": memory_bytes,
                "pids_max": pids_max,
            },
        }

    def destroy_syscall_slice(self, slice_info: Dict[str, Any]) -> None:
        if slice_info.get("mode") != "cgroup_v2_enforced":
            return
        slice_dir = slice_info.get("path")
        if not slice_dir or not os.path.exists(slice_dir):
            return
        self._write_if_exists(os.path.join(slice_dir, "cgroup.kill"), "1\n")
        try:
            os.rmdir(slice_dir)
        except OSError:
            pass

    @staticmethod
    def _write_control(path: str, content: str) -> None:
        # r+ requires a real existing control file instead of creating one.
        with open(path, "r+", encoding="utf-8") as control:
            control.write(content)

    @staticmethod
    def _write_if_exists(path: str, content: str) -> None:
        if os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
