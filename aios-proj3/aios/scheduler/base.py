from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import inspect
from queue import Empty
from threading import Thread
import time
from typing import Any, Callable, Dict, List, Optional
from cerebrum.utils.communication import Response
from aios.scheduler.drf_admission import DRFAdmissionController
from aios.utils.logger import SchedulerLogger

WAKE_SENTINEL = object()


class BaseScheduler(ABC):
    WAKE_SENTINEL = WAKE_SENTINEL
    """
    Abstract base class for AIOS Kernel schedulers.
    Manages the 4 concurrent syscall processing threads (LLM, Memory, Storage, Tool)
    and integrates with the DRF Resource Admission Controller.
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
        drf_controller: Optional[DRFAdmissionController] = None,
    ):
        self.llm = llm
        self.memory_manager = memory_manager
        self.storage_manager = storage_manager
        self.tool_manager = tool_manager

        self.get_llm_syscall = get_llm_syscall
        self.get_memory_syscall = get_memory_syscall
        self.get_storage_syscall = get_storage_syscall
        self.get_tool_syscall = get_tool_syscall

        self.active = False
        self.log_mode = log_mode
        self.logger = self._setup_logger()
        self.processing_threads: Dict[str, Thread] = {}
        self.drf_controller = drf_controller or DRFAdmissionController()
        self._wake_queues: Optional[Callable[[], None]] = None

    def _setup_logger(self) -> SchedulerLogger:
        return SchedulerLogger(self.__class__.__name__, self.log_mode)

    def start_processing_threads(self, processors: List[Callable]) -> None:
        for processor in processors:
            thread_name = processor.__name__
            thread = Thread(target=processor, name=thread_name, daemon=True)
            self.processing_threads[thread_name] = thread
            thread.start()

    @staticmethod
    def _is_wake_sentinel(item: Any) -> bool:
        return item is WAKE_SENTINEL or item is None

    @staticmethod
    def _reader_accepts_timeout(reader: Callable) -> bool:
        try:
            sig = inspect.signature(reader)
        except (TypeError, ValueError):
            return False
        for p in sig.parameters.values():
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.VAR_POSITIONAL):
                return True
        return False

    @staticmethod
    def _cancel_syscall(sc: Any) -> None:
        if not sc.event.is_set():
            sc.set_response(Response(error="Scheduler stopped", status_code=503))
            sc.set_status("cancelled")
            sc.set_end_time(time.time())
            sc.event.set()

    def drain_queues_on_stop(self) -> None:
        """Drain any stale wake sentinels and cancel all accepted pending syscalls with 503."""
        for reader in (
            self.get_llm_syscall,
            self.get_memory_syscall,
            self.get_storage_syscall,
            self.get_tool_syscall,
        ):
            while True:
                try:
                    sc = reader(0.0) if self._reader_accepts_timeout(reader) else reader()
                except Empty:
                    break
                except Exception:
                    break
                if self._is_wake_sentinel(sc):
                    continue
                self._cancel_syscall(sc)

    def stop_processing_threads(self) -> None:
        if self._wake_queues is not None:
            try:
                self._wake_queues()
            except Exception:
                pass
        for thread in list(self.processing_threads.values()):
            if thread.is_alive():
                thread.join(timeout=2.0)
        self.processing_threads.clear()

    def process_parallel_tool_requests(self):
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='aios-tool') as pool:
            running = set()
            while self.active:
                running = {future for future in running if not future.done()}
                if len(running) >= 4:
                    wait(running, timeout=0.05, return_when=FIRST_COMPLETED)
                    continue
                try:
                    syscall = self.get_tool_syscall()
                except Empty:
                    continue
                if self._is_wake_sentinel(syscall):
                    continue
                running.add(pool.submit(self._execute_syscall, syscall, self.tool_manager.address_request, 'Tool'))

    @abstractmethod
    def process_llm_requests(self) -> None:
        pass

    @abstractmethod
    def process_memory_requests(self) -> None:
        pass

    @abstractmethod
    def process_storage_requests(self) -> None:
        pass

    @abstractmethod
    def process_tool_requests(self) -> None:
        pass

    @abstractmethod
    def start(self) -> None:
        pass

    @abstractmethod
    def stop(self) -> None:
        pass
