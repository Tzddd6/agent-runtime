from queue import Empty
from cerebrum.utils.communication import Response
import time
from typing import Any, Callable, Optional

from aios.context.simple_context import SimpleContextManager
from aios.scheduler.base import BaseScheduler
from aios.scheduler.drf_admission import ResourceVector


class RRScheduler(BaseScheduler):
    """
    AIOS Kernel Round-Robin Scheduler with Time-Slice Preemption & Context Switching.
    Gives each LLMSyscall a fixed `time_slice`; if unfinished, SimpleContextManager
    snapshots the partial generation and SyscallExecutor re-enqueues the next slice.
    """

    def __init__(
        self,
        *args,
        time_slice: float = 0.05,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.time_slice = time_slice
        self.context_manager = SimpleContextManager()

    def _execute_syscall(
        self,
        syscall: Any,
        executor: Callable[[Any], Any],
        syscall_type: str,
    ) -> Optional[Any]:
        requested = ResourceVector(**(getattr(syscall.query, 'resources', None) or {}))
        agent = self.drf_controller.get_or_create_agent(syscall.agent_name)
        can_fit = all(getattr(requested, key) <= getattr(self.drf_controller.capacity, key)
                      and getattr(requested, key) <= getattr(agent.resource_limit, key)
                      for key in ('cpu_milli', 'memory_bytes', 'pids'))
        if (can_fit and agent.state != 'terminated' and self.active
                and time.time() - syscall.get_created_time() < 30
                and not self.drf_controller.can_admit(requested, syscall.agent_name)):
            syscall.resubmit(syscall)
            time.sleep(0.001)
            return None
        req_vec = None
        try:
            req_vec = self.drf_controller.acquire(syscall.agent_name, ResourceVector(**(getattr(syscall.query, 'resources', None) or {})))
            syscall.set_status("executing")
            if syscall_type == "LLM" and syscall.get_time_limit() is None:
                syscall.set_time_limit(self.time_slice)
            syscall.set_start_time(time.time())

            response = executor(syscall)
            syscall.set_response(response)
            is_finished = getattr(response, "finished", True)
            syscall.set_status("done" if is_finished else "suspended")
            syscall.set_end_time(time.time())
            return response
        except Exception as exc:
            self.logger.log(f"Error executing {syscall_type} syscall: {type(exc).__name__}", "error")
            syscall.set_response(Response(error="System call execution failed", status_code=500, metadata={"failure_type": type(exc).__name__}))
            syscall.set_status("failed")
            syscall.set_end_time(time.time())
            return None
        finally:
            service = getattr(self.tool_manager, 'process_service', None)
            quarantined = service is not None and syscall.agent_name in service.quarantined_agents
            if req_vec is not None and not quarantined:
                self.drf_controller.release(syscall.agent_name, req_vec)
            syscall.event.set()

    def process_llm_requests(self) -> None:
        while self.active:
            try:
                syscall = self.get_llm_syscall()
            except Empty:
                continue
            if self._is_wake_sentinel(syscall):
                continue
            self._execute_syscall(syscall, self.llm.execute_llm_syscall, "LLM")

    def process_memory_requests(self) -> None:
        while self.active:
            try:
                syscall = self.get_memory_syscall()
            except Empty:
                continue
            if self._is_wake_sentinel(syscall):
                continue
            self._execute_syscall(syscall, self.memory_manager.address_request, "Memory")

    def process_storage_requests(self) -> None:
        while self.active:
            try:
                syscall = self.get_storage_syscall()
            except Empty:
                continue
            if self._is_wake_sentinel(syscall):
                continue
            self._execute_syscall(syscall, self.storage_manager.address_request, "Storage")

    def process_tool_requests(self) -> None:
        self.process_parallel_tool_requests()

    def start(self) -> None:
        self.active = True
        self.start_processing_threads(
            [
                self.process_llm_requests,
                self.process_memory_requests,
                self.process_storage_requests,
                self.process_tool_requests,
            ]
        )

    def stop(self) -> None:
        self.active = False
        self.stop_processing_threads()
        self.drain_queues_on_stop()
