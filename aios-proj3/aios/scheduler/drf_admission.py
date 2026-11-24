# Project module contract / 项目模块说明
# 职责：维护请求/工作流调度及准入；不把远端API响应轮转等同于GPU内核抢占。
# 主要接口：ResourceVector, AgentControlBlock, DRFAdmissionController。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
from dataclasses import dataclass, field
import threading
import time
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ResourceVector:
    cpu_milli: int = 250
    memory_bytes: int = 64 * 1024 * 1024
    pids: int = 4


@dataclass
class AgentControlBlock:
    """
    OS-level Agent Control Block (ACB) (Proj3 Requirement 4.3).
    Models an Agent as a first-class OS citizen with explicit lifecycle state machine
    (created -> ready -> running -> paused -> terminated), OS resource vector bindings,
    concurrency slots, and cumulative model call/token budgets.
    """
    agent_name: str
    state: str = "ready"  # created, ready, running, paused, terminated
    allocated: ResourceVector = field(default_factory=lambda: ResourceVector(0, 0, 0))
    active_tasks: int = 0
    resource_limit: Optional[ResourceVector] = None
    max_concurrent: int = 4
    total_syscalls: int = 0
    model_calls_used: int = 0
    max_model_calls: int = 100
    tokens_used: int = 0
    max_tokens: int = 200_000
    dominant_share: float = 0.0
    cgroup_slices: List[Dict[str, Any]] = field(default_factory=list)
    state_history: List[str] = field(default_factory=lambda: ["created", "ready"])
    last_scheduled_at: float = field(default_factory=time.monotonic)

    def transition_to(self, new_state: str) -> None:
        if self.state == "terminated" and new_state != "terminated":
            raise RuntimeError(f"Terminated agent '{self.agent_name}' cannot transition to '{new_state}'")
        if self.state != new_state:
            self.state = new_state
            self.state_history.append(new_state)


class DRFAdmissionController:
    """
    Dominant Resource Fairness (DRF), Aging & Agent Lifecycle Controller (Proj3 Requirements 4.2 & 4.3).
    """

    def __init__(
        self,
        cpu_milli_budget: int = 4000,
        memory_bytes_budget: int = 1024 * 1024 * 1024,
        pids_budget: int = 128,
        aging_threshold_sec: float = 2.0,
        global_max_model_calls: int = 1000,
        global_max_tokens: int = 2_000_000,
    ):
        self.capacity = ResourceVector(
            cpu_milli=max(1, cpu_milli_budget),
            memory_bytes=max(1, memory_bytes_budget),
            pids=max(1, pids_budget),
        )
        self.used = ResourceVector(0, 0, 0)
        self.aging_threshold_sec = aging_threshold_sec
        self.global_max_model_calls = global_max_model_calls
        self.global_model_calls_used = 0
        self.global_max_tokens = global_max_tokens
        self.global_tokens_used = 0
        self.agents: Dict[str, AgentControlBlock] = {}
        self._lock = threading.RLock()

    def _update_dominant_share(self, acb: AgentControlBlock) -> float:
        cpu_s = acb.allocated.cpu_milli / self.capacity.cpu_milli
        mem_s = acb.allocated.memory_bytes / self.capacity.memory_bytes
        pid_s = acb.allocated.pids / self.capacity.pids
        acb.dominant_share = max(cpu_s, mem_s, pid_s)
        return acb.dominant_share

    def register_agent(
        self,
        agent_name: str,
        max_concurrent: int = 4,
        max_model_calls: int = 100,
        max_tokens: int = 200_000,
        resource_limit: Optional[ResourceVector] = None,
    ) -> AgentControlBlock:
        with self._lock:
            if agent_name not in self.agents:
                self.agents[agent_name] = AgentControlBlock(
                    agent_name=agent_name,
                    resource_limit=resource_limit or ResourceVector(**self.capacity.__dict__),
                    max_concurrent=max_concurrent,
                    max_model_calls=max_model_calls,
                    max_tokens=max_tokens,
                )
            return self.agents[agent_name]

    def get_or_create_agent(self, agent_name: str) -> AgentControlBlock:
        return self.register_agent(agent_name)

    def pause_agent(self, agent_name: str) -> AgentControlBlock:
        with self._lock:
            acb = self.get_or_create_agent(agent_name)
            acb.transition_to("paused")
            return acb

    def set_existing_agent_paused(self, agent_name: str, paused: bool) -> str:
        with self._lock:
            agent = self.agents[agent_name]
            if agent.state == "terminated":
                raise ValueError("Agent 已终止")
            state = "paused" if paused else ("running" if agent.active_tasks else "ready")
            agent.transition_to(state)
            return agent.state

    def resume_agent(self, agent_name: str) -> AgentControlBlock:
        with self._lock:
            acb = self.get_or_create_agent(agent_name)
            acb.transition_to("ready" if acb.active_tasks == 0 else "running")
            return acb

    def terminate_agent(
        self,
        agent_name: str,
        ipc_manager: Any = None,
        cgroup_manager: Any = None,
    ) -> AgentControlBlock:
        """
        Terminate admission and revoke IPC credentials. In-process calls retain
        their reservations until they exit and call release().
        """
        with self._lock:
            acb = self.get_or_create_agent(agent_name)
            acb.transition_to("terminated")
            if ipc_manager is not None:
                ipc_manager.revoke_token(agent_name)
            if cgroup_manager is not None:
                for slice_info in list(acb.cgroup_slices):
                    cgroup_manager.destroy_syscall_slice(slice_info)
                acb.cgroup_slices.clear()
            return acb

    def consume_model_budget(self, agent_name: str, calls: int = 1, tokens: int = 0) -> None:
        with self._lock:
            acb = self.get_or_create_agent(agent_name)
            if acb.state in ("paused", "terminated"):
                raise RuntimeError(f"Agent is {acb.state}")
            if type(calls) is not int or type(tokens) is not int or calls < 0 or tokens < 0:
                raise ValueError("Budget charges must be nonnegative integers")
            if self.global_model_calls_used + calls > self.global_max_model_calls:
                raise RuntimeError("Global kernel model call budget exceeded")
            if self.global_tokens_used + tokens > self.global_max_tokens:
                raise RuntimeError("Global kernel output token budget exceeded")
            if acb.model_calls_used + calls > acb.max_model_calls:
                raise RuntimeError(f"Agent '{agent_name}' model call budget ({acb.max_model_calls}) exceeded")
            if acb.tokens_used + tokens > acb.max_tokens:
                raise RuntimeError(f"Agent '{agent_name}' token budget ({acb.max_tokens}) exceeded")
            self.global_model_calls_used += calls
            self.global_tokens_used += tokens
            acb.model_calls_used += calls
            acb.tokens_used += tokens

    def can_admit(self, req: Optional[ResourceVector] = None, agent_name: Optional[str] = None) -> bool:
        r = req or ResourceVector()
        with self._lock:
            if (
                r.cpu_milli > self.capacity.cpu_milli
                or r.memory_bytes > self.capacity.memory_bytes
                or r.pids > self.capacity.pids
            ):
                return False
            if agent_name is not None:
                acb = self.get_or_create_agent(agent_name)
                if acb.state in ("paused", "terminated"):
                    return False
                if acb.active_tasks >= acb.max_concurrent:
                    return False
                if any(getattr(acb.allocated, key) + getattr(r, key) > getattr(acb.resource_limit, key)
                       for key in ('cpu_milli', 'memory_bytes', 'pids')):
                    return False
            return (
                self.used.cpu_milli + r.cpu_milli <= self.capacity.cpu_milli
                and self.used.memory_bytes + r.memory_bytes <= self.capacity.memory_bytes
                and self.used.pids + r.pids <= self.capacity.pids
            )

    def acquire(self, agent_name: str, req: Optional[ResourceVector] = None) -> ResourceVector:
        r = req or ResourceVector()
        with self._lock:
            acb = self.get_or_create_agent(agent_name)
            if acb.state in ("paused", "terminated"):
                raise RuntimeError(f"Agent '{agent_name}' is {acb.state} and cannot execute syscalls")
            if min(r.cpu_milli, r.memory_bytes, r.pids) < 0 or not self.can_admit(r, agent_name):
                raise RuntimeError("Resource reservation exceeds available capacity or Agent concurrency")
            acb.transition_to("running")
            acb.active_tasks += 1
            self.used.cpu_milli += r.cpu_milli
            self.used.memory_bytes += r.memory_bytes
            self.used.pids += r.pids
            acb.allocated.cpu_milli += r.cpu_milli
            acb.allocated.memory_bytes += r.memory_bytes
            acb.allocated.pids += r.pids
            acb.total_syscalls += 1
            acb.last_scheduled_at = time.monotonic()
            self._update_dominant_share(acb)
            return r

    def release(self, agent_name: str, req: ResourceVector) -> None:
        with self._lock:
            acb = self.get_or_create_agent(agent_name)
            acb.active_tasks = max(0, acb.active_tasks - 1)
            if acb.active_tasks == 0 and acb.state == "running":
                acb.transition_to("ready")
            self.used.cpu_milli = max(0, self.used.cpu_milli - req.cpu_milli)
            self.used.memory_bytes = max(0, self.used.memory_bytes - req.memory_bytes)
            self.used.pids = max(0, self.used.pids - req.pids)
            acb.allocated.cpu_milli = max(0, acb.allocated.cpu_milli - req.cpu_milli)
            acb.allocated.memory_bytes = max(0, acb.allocated.memory_bytes - req.memory_bytes)
            acb.allocated.pids = max(0, acb.allocated.pids - req.pids)
            self._update_dominant_share(acb)

    def select_next_syscall(self, candidates: List[Any]) -> Tuple[int, Any]:
        if not candidates:
            raise ValueError("Cannot select from empty candidate list")
        if len(candidates) == 1:
            return 0, candidates[0]

        now = time.time()
        with self._lock:
            aged_indices = [
                idx
                for idx, sc in enumerate(candidates)
                if (now - (sc.get_created_time() or now)) >= self.aging_threshold_sec
            ]
            pool = aged_indices if aged_indices else list(range(len(candidates)))

            def drf_key(idx: int) -> Tuple[float, float]:
                sc = candidates[idx]
                acb = self.get_or_create_agent(sc.agent_name)
                return (self._update_dominant_share(acb), sc.get_created_time() or now)

            best_idx = min(pool, key=drf_key)
            return best_idx, candidates[best_idx]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "capacity": dict(self.capacity.__dict__),
                "used": dict(self.used.__dict__),
                "global_model_calls": {
                    "used": self.global_model_calls_used,
                    "limit": self.global_max_model_calls,
                },
                "global_output_tokens": {"used": self.global_tokens_used, "limit": self.global_max_tokens, "accounting": "reserved_ceiling"},
                "agents": {
                    name: {
                        "state": acb.state,
                        "state_history": list(acb.state_history),
                        "active_tasks": acb.active_tasks,
                        "allocated": dict(acb.allocated.__dict__),
                        "resource_limit": dict(acb.resource_limit.__dict__),
                        "dominant_share": round(acb.dominant_share, 4),
                        "total_syscalls": acb.total_syscalls,
                        "model_calls_used": acb.model_calls_used,
                        "tokens_used": acb.tokens_used,
                    }
                    for name, acb in self.agents.items()
                },
            }
