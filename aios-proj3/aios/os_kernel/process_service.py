"""Kernel-owned Worker execution with bounded input and verified cleanup."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from threading import RLock
import time
from uuid import uuid4

from aios.os_kernel.executor import Executor


class ProcessService:
    OPERATIONS = {'text.stats', 'math.calculate', 'project.test', 'project.patch_check', 'runtime.load', 'web.fetch'}

    def __init__(self, root, cgroup_root=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.executor = Executor(cgroup_root=cgroup_root)
        if self.executor.root and any(path.is_dir() for path in self.executor.root.iterdir()):
            raise RuntimeError("Delegated Worker subtree contains unreconciled groups")
        self.lock = RLock()
        self.active = {}
        self.closed = False
        self.terminated = set()
        self.quarantined_agents = set()
        self.receipts = []
        self._receipt_captures = {}

    def run(self, agent, operation, parameters, resources=None, timeout=10, receipt_observer=None):
        if operation not in self.OPERATIONS:
            raise ValueError('Operation is not in the isolated allowlist')
        if not isinstance(parameters, dict) or len(json.dumps(parameters).encode()) > 512 * 1024:
            raise ValueError('Invalid operation parameters')
        if not 0 < timeout <= 60:
            raise ValueError('Invalid Worker timeout')
        limits = {'cpu_milli': 250, 'memory_bytes': 64 * 1024 * 1024, 'pids': 4}
        limits.update(resources or {})
        identity = uuid4().hex
        directory = self.root / identity / 'attempt-1'
        handle = None
        with self.lock:
            if self.closed or agent in self.terminated or agent in self.quarantined_agents:
                raise RuntimeError('Worker admission is closed')
            if self.executor.root and agent not in self.executor.agent_roots:
                group = self.executor.root / ('agent_' + sha256(agent.encode()).hexdigest()[:24])
                group.mkdir(exist_ok=True)
                quota = self.admission.get_or_create_agent(agent).resource_limit
                for name, value in {'cpu.max': f'{quota.cpu_milli * 100} 100000',
                                    'memory.max': quota.memory_bytes, 'memory.swap.max': 0,
                                    'pids.max': quota.pids}.items():
                    (group / name).write_text(str(value))
                (group / 'cgroup.subtree_control').write_text('+cpu +memory +pids')
                self.executor.agent_roots[agent] = group
            handle = self.executor.launch({'id': identity, 'agent': agent, 'operation': operation,
                                           'parameters': parameters, 'timeout': timeout}, directory, limits)
            self.active[identity] = (agent, handle)
        pid = handle['proc'].pid
        try:
            handle['proc'].wait(timeout=timeout + 6)
            if handle['expired'].is_set():
                raise TimeoutError('Worker deadline exceeded')
            path = directory / 'result.json'
            if handle['proc'].returncode or not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise RuntimeError('Worker did not return a valid result')
            result = json.loads(path.read_text(encoding='utf-8'))
            if result.get('task_id') != identity:
                raise RuntimeError('Worker result identity mismatch')
            return result['result']
        finally:
            # Reservations must remain held until descendants have been reclaimed.
            try:
                self.executor.cleanup(handle)
            except Exception:
                with self.lock:
                    self.quarantined_agents.add(agent)
                raise RuntimeError('Worker cleanup unconfirmed; Agent resources remain reserved') from None
            with self.lock:
                self.active.pop(identity, None)
                agent_cg = self.executor.agent_roots.get(agent)
                cg_path = str(handle['group']) if handle.get('group') else (
                    str(agent_cg) if agent_cg else f"process_group://isolated/agent_{agent}/pid_{pid}"
                )
                self.receipts.append({'id': identity, 'agent': agent, 'pid': pid,
                                      'operation': operation, 'reclaimed': True,
                                      'cgroup_path': cg_path,
                                      'enforcement': 'cgroup_v2' if handle['group'] else 'process_group',
                                      'usage': handle.get('_usage', {}), 'finished': time.time()})
                for capture in self._receipt_captures.values():
                    capture.append(deepcopy(self.receipts[-1]))
                if receipt_observer is not None:
                    receipt_observer(dict(self.receipts[-1]))
                del self.receipts[:-100]

    @contextmanager
    def capture_receipts(self):
        """Per-workflow evidence independent of the bounded recent-receipt ring."""
        identity, capture = uuid4().hex, []
        with self.lock:
            self._receipt_captures[identity] = capture
        try:
            yield capture
        finally:
            with self.lock:
                self._receipt_captures.pop(identity, None)

    def terminate(self, agent):
        with self.lock:
            self.terminated.add(agent)
            for owner, handle in self.active.values():
                if owner == agent:
                    self.executor._signal(handle)

    def snapshot(self):
        with self.lock:
            return {'active': len(self.active), 'quarantined_agents': sorted(self.quarantined_agents), 'receipts': deepcopy(self.receipts)}

    def close(self):
        with self.lock:
            self.closed = True
            for _, handle in self.active.values():
                self.executor._signal(handle)
            if self.active:
                raise RuntimeError('Workers must drain before closing process service')
            for group in self.executor.agent_roots.values():
                group.rmdir()
            self.executor.close()
