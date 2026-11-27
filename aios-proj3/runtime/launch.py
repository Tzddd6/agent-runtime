import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from queue import Queue
import threading
from typing import Any, Dict, Optional

from cerebrum.llm.apis import LLMQuery, LLMResponse
from cerebrum.memory.apis import MemoryQuery
from cerebrum.storage.apis import StorageQuery
from cerebrum.tool.apis import ToolQuery
from cerebrum.interface import AutoTool

from aios.config.config_manager import config
from aios.context.context_store import ContextStore
from aios.hooks.modules.llm import useCore
from aios.hooks.modules.memory import useMemoryManager
from aios.hooks.modules.storage import useStorageManager
from aios.hooks.modules.tool import useToolManager
from aios.hooks.modules.scheduler import fifo_scheduler_nonblock, rr_scheduler_nonblock
from aios.hooks.stores.queue import get_message
from aios.ipc.mailbox import IPCManager
from aios.memory.context_injector import ContextInjector
from aios.os_kernel.cgroup_v2 import CgroupV2Manager
from aios.os_kernel.process_service import ProcessService
from aios.os_kernel.worker_supervisor import WorkerSupervisor
from aios.scheduler.drf_admission import ModelBudgetExceeded
from aios.scheduler.workflow_engine import WorkflowEngine
from aios.syscall.syscall import SyscallExecutor


class TaskScope:
    """Per-task isolated Agent, budget, IPC token, and ephemeral context scope on a resident kernel."""

    def __init__(
        self,
        kernel: "AIOSKernelRuntime",
        agent_name: str,
        resource_limit: Optional[Any] = None,
        max_model_calls: int = 30,
        max_tokens: int = 100_000,
    ):
        self.kernel = kernel
        self.agent_name = agent_name
        self.resource_limit = resource_limit
        self.max_model_calls = max_model_calls
        self.max_tokens = max_tokens
        self.ipc_token: Optional[str] = None
        self.ephemeral_contexts: list[str] = []

    def __enter__(self) -> "TaskScope":
        with self.kernel._scope_lock:
            if self.agent_name in self.kernel._active_scopes:
                raise ValueError(f"Agent scope '{self.agent_name}' is already active")
            self.kernel._active_scopes.add(self.agent_name)
        try:
            self.kernel.scheduler.drf_controller.register_agent(
                self.agent_name,
                resource_limit=self.resource_limit,
                max_model_calls=self.max_model_calls,
                max_tokens=self.max_tokens,
            )
            self.ipc_token = self.kernel.ipc_manager.issue_token(self.agent_name)
            return self
        except Exception:
            with self.kernel._scope_lock:
                self.kernel._active_scopes.discard(self.agent_name)
            raise

    def put_context(self, data: Any, authorized_agents: Optional[list[str]] = None) -> str:
        ctx_id = self.kernel.context_store.put(self.agent_name, data, authorized_agents=authorized_agents)
        if ctx_id not in self.ephemeral_contexts:
            self.ephemeral_contexts.append(ctx_id)
        return ctx_id

    def execute(self, query_obj: Any) -> Dict[str, Any]:
        return self.kernel.execute_request(self.agent_name, query_obj)

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if self.ipc_token is not None:
                self.kernel.ipc_manager.revoke_token(self.agent_name)
                self.ipc_token = None
            for ctx_id in self.ephemeral_contexts:
                try:
                    self.kernel.context_store.delete(ctx_id, requester_agent=self.agent_name)
                except (PermissionError, OSError):
                    pass
            self.ephemeral_contexts.clear()
            if hasattr(self.kernel.context_store, "release_agent"):
                self.kernel.context_store.release_agent(self.agent_name)
            self.kernel.scheduler.drf_controller.terminate_agent(
                self.agent_name, ipc_manager=self.kernel.ipc_manager
            )
        finally:
            with self.kernel._scope_lock:
                self.kernel._active_scopes.discard(self.agent_name)


class AIOSKernelRuntime:
    """
    AIOS Formal Kernel Daemon.
    Bootstraps and coordinates LLM Core, MemoryManager (with WriteBarrier & ContextInjector),
    StorageManager (LSFS), ToolManager (AutoTool & MCP), Multi-Queue Scheduler (FIFO / RR + DRF),
    and Linux cgroup v2 resource controller.
    """

    def __init__(
        self,
        scheduler_policy: str = "fifo",
        storage_root: Optional[str] = None,
        memory_limit: int = 104857600,
        eviction_k: int = 2,
        batch_interval: float = 0.02,
        time_slice: float = 0.05,
        cgroup_root: Optional[str] = None,
        log_mode: str = "silent",
    ):
        self.log_mode = log_mode
        self.scheduler_policy = scheduler_policy.lower()
        storage_cfg = config.get_storage_config()
        self.storage_root = storage_root or storage_cfg.get("root_dir", "root_storage")

        llm_cfg = config.get_llms_config().get("models") or [
            {"name": "mock-aios-core", "backend": "mock"}
        ]
        self.llm = useCore(llm_configs=llm_cfg, log_mode=log_mode, use_context_manager=True)
        self.memory_manager = useMemoryManager(
            memory_limit=memory_limit,
            eviction_k=eviction_k,
            log_mode=log_mode,
        )
        self.storage_manager = useStorageManager(
            root_dir=self.storage_root,
            use_vector_db=bool(storage_cfg.get("use_vector_db", False)),
            filesystem_type=storage_cfg.get("filesystem_type", "lsfs"),
        )
        self.tool_manager = useToolManager(log_mode=log_mode, autostart_mcp=False)
        self.cgroup_manager = CgroupV2Manager(cgroup_root=cgroup_root, strict=False)
        # Explicit Linux delegation must be validated by the strict executor.
        # Invalid paths must never silently downgrade to process-only isolation.
        self.process_service = ProcessService(
            f"{self.storage_root}_workers",
            cgroup_root=cgroup_root if self.cgroup_manager.is_linux else None,
        )
        self.worker_supervisor = WorkerSupervisor(f"{self.storage_root}_resident_workers")
        self.tool_manager.process_service = self.process_service
        self.tool_manager.kernel = self
        self.context_injector = ContextInjector(self.memory_manager)
        self.context_store = ContextStore(
            storage_dir=f"{self.storage_root}_contexts",
            persistent_connection=True,
        )
        self.ipc_manager = IPCManager(context_store=self.context_store)

        self.queues = {name: Queue() for name in ("llm", "memory", "storage", "tool")}
        self._scope_lock = threading.Lock()
        self._active_scopes: set[str] = set()
        self.syscall_executor = SyscallExecutor(submit=lambda syscall: self.queues[syscall.target].put(syscall))
        self.syscall_executor.memory_manager = self.memory_manager
        self.syscall_executor.context_injector = self.context_injector

        def reader(queue):
            return lambda timeout=0.05: get_message(queue, timeout=timeout)

        scheduler, options = ((rr_scheduler_nonblock, {"time_slice": time_slice})
                              if self.scheduler_policy == "rr" else
                              (fifo_scheduler_nonblock, {"batch_interval": batch_interval}))
        self.scheduler = scheduler(
            llm=self.llm,
            memory_manager=self.memory_manager,
            storage_manager=self.storage_manager,
            tool_manager=self.tool_manager,
            log_mode=log_mode,
            **{f"get_{name}_syscall": reader(queue) for name, queue in self.queues.items()},
            **options,
        )
        self.scheduler._wake_queues = lambda: [q.put(self.scheduler.WAKE_SENTINEL) for q in self.queues.values()]
        self.process_service.admission = self.scheduler.drf_controller
        self.workflow_engine = WorkflowEngine(
            self,
            checkpoint_dir=f"{self.storage_root}_checkpoints",
        )

    def task_scope(
        self,
        agent_name: str,
        resource_limit: Optional[Any] = None,
        max_model_calls: int = 30,
        max_tokens: int = 100_000,
    ) -> TaskScope:
        """Create an isolated per-task scope on this resident kernel with automatic cleanup."""
        return TaskScope(
            self,
            agent_name=agent_name,
            resource_limit=resource_limit,
            max_model_calls=max_model_calls,
            max_tokens=max_tokens,
        )

    def start(self) -> "AIOSKernelRuntime":
        if not self.scheduler.active:
            self.scheduler.start()
        return self

    def stop(self) -> None:
        if self.scheduler.active:
            self.scheduler.stop()
        self.worker_supervisor.shutdown()
        self.process_service.close()
        self.tool_manager.cleanup()
        self.context_store.close()

    def __enter__(self) -> "AIOSKernelRuntime":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def execute_request(self, agent_name: str, query: Any) -> Dict[str, Any]:
        if not self.scheduler.active:
            raise RuntimeError("Kernel scheduler is stopped")
        if agent_name in self.process_service.quarantined_agents:
            raise RuntimeError("Agent cleanup remains unconfirmed")
        if isinstance(query, LLMQuery):
            limit = query.max_new_tokens
            if type(limit) is not int or limit < 1:
                raise ValueError("max_new_tokens must be a positive integer")
            try:
                self.scheduler.drf_controller.consume_model_budget(agent_name, calls=1, tokens=limit)
            except ModelBudgetExceeded as exc:
                return {"response": LLMResponse(error=str(exc), status_code=429,
                                                error_code=f"{exc.scope}_model_budget")}
            except RuntimeError as exc:
                return {"response": LLMResponse(error=str(exc), status_code=429)}
            out = self.syscall_executor.execute_request(agent_name, query)
            resp = out.get("response")
            usage = getattr(resp, "usage", None) or {}
            in_tok = usage.get("prompt_tokens") or usage.get("input_tokens") or max(1, len(json.dumps(query.messages, ensure_ascii=False)) // 4)
            out_tok = usage.get("completion_tokens") or usage.get("output_tokens") or usage.get("total_tokens") or max(1, len(str(getattr(resp, "response_message", "") or "")) // 4)
            self.scheduler.drf_controller.record_token_usage(agent_name, input_tokens=in_tok, output_tokens=out_tok)
            return out
        return self.syscall_executor.execute_request(agent_name, query)

    @staticmethod
    def parse_query_dict(query_type: str, query_data: Dict[str, Any], user_id: Optional[str] = None):
        data = dict(query_data or {})
        data.pop("query_class", None)
        if user_id and not data.get("user_id") and query_type in ("llm", "memory"):
            data["user_id"] = user_id

        mapping = {
            "llm": LLMQuery,
            "memory": MemoryQuery,
            "storage": StorageQuery,
            "tool": ToolQuery,
        }
        target_cls = mapping.get(query_type)
        if target_cls is None:
            raise ValueError(f"Unsupported query_type: {query_type}")
        return target_cls(**data)

    def handle_query_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        agent_name = str(payload.get("agent_name") or "anonymous_agent")
        query_type = str(payload.get("query_type") or "").lower()
        query_data = payload.get("query_data") or {}
        user_id = payload.get("user_id") or (
            query_data.get("user_id") if isinstance(query_data, dict) else None
        )

        query_obj = self.parse_query_dict(query_type, query_data, user_id=user_id)
        exec_result = self.execute_request(agent_name, query_obj)
        resp_obj = exec_result.get("response")
        resp_dict = resp_obj.to_dict() if hasattr(resp_obj, "to_dict") else {"response_message": str(resp_obj)}
        return {
            "syscall_id": exec_result.get("syscall_id"),
            "agent_name": agent_name,
            "query_type": query_type,
            "response": resp_dict,
            "metrics": {
                "start_times": exec_result.get("start_times", []),
                "end_times": exec_result.get("end_times", []),
                "waiting_times": exec_result.get("waiting_times", []),
                "turnaround_times": exec_result.get("turnaround_times", []),
                "slices": len(exec_result.get("start_times", [])),
            },
        }

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "kernel": "AIOS",
            "active": self.scheduler.active,
            "scheduler_policy": self.scheduler_policy,
            "components": {
                "llm": [m.get("name") for m in self.llm.llm_configs],
                "memory_provider": self.memory_manager.provider_name,
                "memory_limit_bytes": self.memory_manager.memory_limit,
                "storage_filesystem": self.storage_manager.filesystem_type,
                "storage_root": self.storage_manager.root_dir,
                "tools": list(AutoTool.list_tools().keys()),
                "cgroup_v2_enabled": self.cgroup_manager.enabled,
            },
            "drf_resources": self.scheduler.drf_controller.snapshot(),
            "context_store": self.context_store.stats(),
            "ipc_mailboxes": self.ipc_manager.snapshot(),
            "processes": self.process_service.snapshot(),
            "total_syscalls": self.syscall_executor.id,
            "queues": {name: queue.qsize() for name, queue in self.queues.items()},
        }


def create_http_server(
    kernel: AIOSKernelRuntime,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> ThreadingHTTPServer:
    class KernelRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            if kernel.log_mode != "silent":
                super().log_message(format, *args)

        def _send_json(self, code: int, body: Dict[str, Any]) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path in ("/", "/core/status", "/status"):
                self._send_json(200, kernel.status_snapshot())
            elif self.path.startswith("/core/syscalls") or self.path.startswith("/syscalls"):
                self._send_json(200, {"syscalls": kernel.syscall_executor.get_history(limit=100)})
            else:
                self._send_json(404, {"error": f"Unknown endpoint: {self.path}"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            try:
                payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                self._send_json(400, {"error": f"Invalid JSON: {exc}"})
                return

            if self.path == "/query":
                try:
                    result = kernel.handle_query_payload(payload)
                    self._send_json(200, result)
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})
            else:
                self._send_json(404, {"error": f"Unknown POST endpoint: {self.path}"})

    return ThreadingHTTPServer((host, port), KernelRequestHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch AIOS Formal Kernel Daemon")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--policy", choices=["fifo", "rr"], default="fifo", help="Scheduler policy")
    parser.add_argument("--storage-root", default="root_storage", help="LSFS root directory")
    parser.add_argument("--cgroup-root", help="Delegated cgroup v2 subtree")
    args = parser.parse_args()

    kernel = AIOSKernelRuntime(
        scheduler_policy=args.policy,
        storage_root=args.storage_root,
        cgroup_root=args.cgroup_root,
        log_mode="console",
    ).start()
    server = create_http_server(kernel, host=args.host, port=args.port)
    print(f"AIOS Kernel Server listening on http://{args.host}:{args.port} (policy={args.policy})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        kernel.stop()


if __name__ == "__main__":
    main()
