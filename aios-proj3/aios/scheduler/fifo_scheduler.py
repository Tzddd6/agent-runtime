from concurrent.futures import ThreadPoolExecutor
import inspect
from queue import Empty
import threading
from cerebrum.utils.communication import Response
import time
from typing import Any, Callable, List, Optional

from aios.scheduler.base import BaseScheduler
from aios.scheduler.drf_admission import ResourceVector


class FIFOScheduler(BaseScheduler):
    """
    AIOS Kernel FIFO Scheduler with Time-Windowed LLM Batching and DRF Fairness.
    """

    def __init__(
        self,
        llm: Any,
        memory_manager: Any,
        storage_manager: Any,
        tool_manager: Any,
        log_mode: str,
        get_llm_syscall: Callable,
        get_memory_syscall: Callable,
        get_storage_syscall: Callable,
        get_tool_syscall: Callable,
        batch_interval: float = 0.02,
        drf_controller: Any = None,
        cloud_concurrency: int = 8,
    ):
        super().__init__(
            llm,
            memory_manager,
            storage_manager,
            tool_manager,
            log_mode,
            get_llm_syscall,
            get_memory_syscall,
            get_storage_syscall,
            get_tool_syscall,
            drf_controller=drf_controller,
        )
        self.batch_interval = batch_interval
        self._cloud_pool: Optional[ThreadPoolExecutor] = None
        self._cloud_concurrency = max(1, int(cloud_concurrency))
        self._cloud_sem = threading.BoundedSemaphore(self._cloud_concurrency)

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
            self.logger.log(
                f"{syscall.agent_name} is executing {syscall_type} syscall.",
                "executing",
            )
            syscall.set_start_time(time.time())

            response = executor(syscall)
            syscall.set_response(response)
            syscall.set_status("done")
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

    def _reader_accepts_timeout(self, reader: Callable) -> bool:
        try:
            sig = inspect.signature(reader)
        except (TypeError, ValueError):
            return False
        for p in sig.parameters.values():
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
                return True
        return False

    def _pop_llm_syscall(self, timeout: Optional[float] = None) -> Any:
        if timeout is None or not self._reader_accepts_timeout(self.get_llm_syscall):
            return self.get_llm_syscall()
        return self.get_llm_syscall(timeout)

    def _is_cloud_api_syscall(self, syscall: Any) -> bool:
        query = getattr(syscall, "query", None)
        if query is None:
            return False
        if getattr(self.llm, "model_service", None) is not None and getattr(query, "model", None) != "mock-aios-core":
            return True
        cfg = getattr(syscall, "selected_model_config", None)
        if isinstance(cfg, dict) and (cfg.get("backend") or "").lower() in ("openai", "deepseek", "aizex"):
            return True
        return False

    def _run_cloud_syscall(self, sc: Any, syscall_type: str) -> None:
        try:
            self._execute_syscall(sc, self.llm.execute_llm_syscall, syscall_type)
        finally:
            self._cloud_sem.release()

    def _execute_batch_syscalls(
        self,
        batch: List[Any],
        syscall_type: str,
    ) -> None:
        pending = list(batch)
        while pending:
            idx, sc = self.drf_controller.select_next_syscall(pending)
            pending.pop(idx)
            if syscall_type == "LLM" and self._is_cloud_api_syscall(sc) and self._cloud_pool is not None:
                # Apply backpressure so executor backlog never grows unbounded under burst load.
                while self.active and not self._cloud_sem.acquire(timeout=0.05):
                    continue
                if not self.active:
                    sc.set_response(Response(error="Scheduler stopped", status_code=503))
                    sc.set_status("failed")
                    sc.event.set()
                    continue
                try:
                    self._cloud_pool.submit(self._run_cloud_syscall, sc, syscall_type)
                except Exception:
                    self._cloud_sem.release()
                    raise
            else:
                self._execute_syscall(sc, self.llm.execute_llm_syscall, syscall_type)

    def process_llm_requests(self) -> None:
        while self.active:
            batch: List[Any] = []
            try:
                first = self._pop_llm_syscall(0.05)
                if self._is_wake_sentinel(first):
                    continue
                batch.append(first)
            except Empty:
                continue

            if self._is_cloud_api_syscall(first):
                # Cloud API calls have no local GPU batching benefit: drain only already-queued items with zero idle wait.
                while True:
                    try:
                        nxt = self._pop_llm_syscall(0.0)
                        if self._is_wake_sentinel(nxt):
                            continue
                        batch.append(nxt)
                    except Empty:
                        break
            elif self.batch_interval > 0:
                deadline = time.monotonic() + self.batch_interval
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        nxt = self._pop_llm_syscall(remaining)
                        if self._is_wake_sentinel(nxt):
                            if not self.active:
                                break
                            continue
                        batch.append(nxt)
                    except Empty:
                        break

            self._execute_batch_syscalls(batch, "LLM")

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
        if self._cloud_pool is None:
            self._cloud_pool = ThreadPoolExecutor(max_workers=self._cloud_concurrency, thread_name_prefix="aios-llm-io")
        self.start_processing_threads(
            [
                self.process_llm_requests,
                self.process_memory_requests,
                self.process_storage_requests,
                self.process_tool_requests,
            ]
        )

    def _cancel_syscall(self, sc: Any) -> None:
        if not sc.event.is_set():
            sc.set_response(Response(error="Scheduler stopped", status_code=503))
            sc.set_status("cancelled")
            sc.set_end_time(time.time())
            sc.event.set()

    def stop(self) -> None:
        self.active = False
        self.stop_processing_threads()
        if self._cloud_pool is not None:
            self._cloud_pool.shutdown(wait=True)
            self._cloud_pool = None
        self.drain_queues_on_stop()
