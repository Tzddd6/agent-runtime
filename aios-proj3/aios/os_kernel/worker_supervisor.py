"""
Resident OS Worker Supervisor with Unix Domain Socket (Linux) / Loopback Socket (Windows),
Heartbeat Monitoring, RSS Telemetry, and Bounded Crash Auto-Restart (Proj3 Design 4.3 / 4.6).
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read_self_rss_bytes() -> Optional[int]:
    if sys.platform != "linux":
        return None
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


def _worker_main(transport: str, endpoint: str) -> None:
    """Entry point executed inside each independent resident OS Worker process."""
    sys.path.insert(0, str(PROJECT_ROOT))
    from aios.tool.worker import execute as run_operation

    sock = None
    if transport == "unix":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(endpoint)
        rfile = sock.makefile("r", encoding="utf-8")
        wfile = sock.makefile("w", encoding="utf-8")
    else:
        rfile = sys.stdin
        wfile = sys.stdout

    try:
        for line in rfile:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                op = req.get("op")
                if op == "ping":
                    resp = {"ok": True, "pid": os.getpid(), "rss_bytes": _read_self_rss_bytes(), "ts": time.time()}
                elif op == "shutdown":
                    wfile.write(json.dumps({"ok": True, "bye": True}) + "\n")
                    wfile.flush()
                    return
                elif op == "exec":
                    result = run_operation(req["operation"], req.get("params") or {})
                    resp = {"ok": True, "result": result, "pid": os.getpid()}
                else:
                    resp = {"ok": False, "error": f"Unknown worker op: {op}"}
            except Exception as exc:
                resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            wfile.write(json.dumps(resp, ensure_ascii=False) + "\n")
            wfile.flush()
    finally:
        if sock is not None:
            for fh in (rfile, wfile, sock):
                try:
                    fh.close()
                except OSError:
                    pass


class ResidentWorker:
    """Handle for one supervised resident OS Worker process."""

    def __init__(self, agent_name: str, work_dir: Path, max_restarts: int = 3):
        self.agent_name = agent_name
        self.work_dir = work_dir
        self.max_restarts = max_restarts
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.conn: Optional[socket.socket] = None
        self.rfile = None
        self.wfile = None
        self.sock_path: Optional[Path] = None
        self.transport = "unix" if hasattr(socket, "AF_UNIX") and sys.platform == "linux" else "pipe"
        self.restarts = 0
        self.heartbeats = 0
        self.last_heartbeat = 0.0
        self.last_rss_bytes: Optional[int] = None
        self.state = "created"

    def start(self) -> int:
        with self.lock:
            return self._spawn_locked()

    def _spawn_locked(self) -> int:
        self._close_locked()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if "API_KEY" not in k.upper()}
        env["PYTHONPATH"] = str(PROJECT_ROOT)

        if self.transport == "unix":
            self.sock_path = self.work_dir / f"worker_{self.agent_name}_{os.getpid()}.sock"
            if self.sock_path.exists():
                self.sock_path.unlink(missing_ok=True)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(self.sock_path))
            srv.listen(1)
            srv.settimeout(8.0)
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, "-m", "aios.os_kernel.worker_supervisor", "unix", str(self.sock_path)],
                    cwd=str(PROJECT_ROOT),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                conn, _ = srv.accept()
                conn.settimeout(15.0)
                self.conn = conn
                self.rfile = conn.makefile("r", encoding="utf-8")
                self.wfile = conn.makefile("w", encoding="utf-8")
            except Exception:
                self._close_locked()
                raise
            finally:
                try:
                    srv.close()
                except OSError:
                    pass
        else:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "aios.os_kernel.worker_supervisor", "pipe", "stdio"],
                cwd=str(PROJECT_ROOT),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            self.rfile = self.proc.stdout
            self.wfile = self.proc.stdin
        self.state = "running"
        self.last_heartbeat = time.time()
        return self.proc.pid

    def _rpc_locked(self, payload: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        if self.proc is None or self.proc.poll() is not None or self.wfile is None or self.rfile is None:
            raise ConnectionError("Worker process is not running")
        if self.conn is not None:
            self.conn.settimeout(timeout)
        self.wfile.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.wfile.flush()
        line = self.rfile.readline()
        if not line:
            raise ConnectionError("Worker closed connection unexpectedly")
        return json.loads(line)

    def ping(self) -> Dict[str, Any]:
        with self.lock:
            try:
                resp = self._rpc_locked({"op": "ping"}, timeout=3.0)
                if resp.get("ok"):
                    self.heartbeats += 1
                    self.last_heartbeat = time.time()
                    self.last_rss_bytes = resp.get("rss_bytes")
                    self.state = "running"
                    return resp
            except Exception:
                pass
            return self._recover_locked()

    def _recover_locked(self) -> Dict[str, Any]:
        if self.restarts >= self.max_restarts:
            self.state = "failed"
            raise RuntimeError(f"Worker '{self.agent_name}' exceeded max_restarts ({self.max_restarts})")
        self.restarts += 1
        new_pid = self._spawn_locked()
        return {"ok": True, "restarted": True, "pid": new_pid, "restarts": self.restarts}

    def execute(self, operation: str, params: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._recover_locked()
            try:
                resp = self._rpc_locked({"op": "exec", "operation": operation, "params": params}, timeout=timeout)
            except Exception:
                # Crash detected during in-flight execution: restart worker for next task, do NOT silently replay!
                self._recover_locked()
                raise RuntimeError(f"Worker '{self.agent_name}' crashed during execution and was restarted")
            if not resp.get("ok"):
                raise RuntimeError(resp.get("error") or "Worker execution failed")
            return resp["result"]

    def _close_locked(self) -> None:
        if self.wfile is not None and self.proc is not None and self.proc.poll() is None:
            try:
                self.wfile.write('{"op":"shutdown"}\n')
                self.wfile.flush()
            except OSError:
                pass
        for fh in (self.rfile, self.wfile, self.conn):
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
        self.rfile = self.wfile = self.conn = None
        if self.proc is not None:
            try:
                self.proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except OSError:
                pass
            self.proc = None
        if self.sock_path is not None and self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except OSError:
                pass
        self.state = "stopped"

    def stop(self) -> None:
        with self.lock:
            self._close_locked()

    def info(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "agent": self.agent_name,
                "pid": self.proc.pid if self.proc and self.proc.poll() is None else None,
                "transport": self.transport,
                "state": self.state,
                "heartbeats": self.heartbeats,
                "restarts": self.restarts,
                "last_rss_bytes": self.last_rss_bytes,
            }


class WorkerSupervisor:
    """Manages resident OS Worker processes with periodic heartbeat supervision and crash recovery."""

    def __init__(self, work_dir: str | Path, heartbeat_interval: float = 2.0, max_restarts: int = 3):
        self.work_dir = Path(work_dir)
        self.heartbeat_interval = heartbeat_interval
        self.max_restarts = max_restarts
        self._lock = threading.Lock()
        self._workers: Dict[str, ResidentWorker] = {}
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

    def _ensure_monitor(self) -> None:
        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._heartbeat_loop, name="aios-worker-supervisor", daemon=True
            )
            self._monitor_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.heartbeat_interval):
            with self._lock:
                workers = list(self._workers.values())
            for w in workers:
                if self._stop_event.is_set():
                    break
                try:
                    w.ping()
                except Exception:
                    pass

    def ensure_worker(self, agent_name: str) -> ResidentWorker:
        with self._lock:
            w = self._workers.get(agent_name)
            if w is None:
                w = ResidentWorker(agent_name, self.work_dir, max_restarts=self.max_restarts)
                w.start()
                self._workers[agent_name] = w
                self._ensure_monitor()
            return w

    def execute(self, agent_name: str, operation: str, params: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
        worker = self.ensure_worker(agent_name)
        return worker.execute(operation, params, timeout=timeout)

    def check_health(self, agent_name: str) -> Dict[str, Any]:
        worker = self.ensure_worker(agent_name)
        return worker.ping()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {name: w.info() for name, w in self._workers.items()}

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2)
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            w.stop()


if __name__ == "__main__":
    if len(sys.argv) == 3:
        _worker_main(sys.argv[1], sys.argv[2])
