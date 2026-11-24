# Project module contract / 项目模块说明
# 职责：管理受控Worker、资源限制、遥测与回收；严格隔离要求有效Linux委派子树。
# 主要接口：ProcessService。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
"""Kernel-owned Worker execution with bounded input and verified cleanup."""
from hashlib import sha256
from copy import deepcopy
import json
from pathlib import Path
from threading import RLock
import time
from uuid import uuid4

from aios.os_kernel.executor import Executor


class ProcessService:
    OPERATIONS = {'text.stats', 'math.calculate', 'project.test', 'project.patch_check', 'project.workspace_check', 'runtime.load', 'web.fetch'}

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
            handle['operation'] = operation
            handle['limits'] = dict(limits)
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
                self.receipts.append({'id': identity, 'agent': agent, 'pid': pid,
                                      'operation': operation, 'reclaimed': True,
                                      'enforcement': 'cgroup_v2' if handle['group'] else 'process_group',
                                      'cgroup': str(handle['group']) if handle['group'] else None,
                                      'limits': dict(limits),
                                      'usage': handle.get('_usage', {}), 'finished': time.time()})
                if receipt_observer is not None:
                    receipt_observer(dict(self.receipts[-1]))
                del self.receipts[:-100]

    def terminate(self, agent):
        with self.lock:
            self.terminated.add(agent)
            for owner, handle in self.active.values():
                if owner == agent:
                    self.executor._signal(handle)

    def snapshot(self):
        with self.lock:
            workers = []
            for identity, (agent, handle) in self.active.items():
                sample = {}
                if handle['group']:
                    for name in ('cpu.stat', 'memory.current', 'pids.current'):
                        try: sample[name] = (handle['group']/name).read_text()
                        except OSError: sample[name] = None
                workers.append({'id': identity, 'agent': agent, 'pid': handle['proc'].pid,
                                'operation': handle['operation'], 'limits': handle['limits'],
                                'cgroup': str(handle['group']) if handle['group'] else None, 'usage': sample})
            return deepcopy({'active': len(self.active), 'active_workers': workers,
                             'quarantined_agents': sorted(self.quarantined_agents), 'receipts': list(self.receipts)})

    def release_agent(self, agent):
        """Release an idle role's OS slice; never release an in-flight worker."""
        with self.lock:
            if any(owner == agent for owner, _ in self.active.values()) or agent in self.quarantined_agents:
                raise RuntimeError('Cannot release an active or quarantined Agent')
            group = self.executor.agent_roots.get(agent)
            if group is not None:
                group.rmdir()
                self.executor.agent_roots.pop(agent)
            self.terminated.add(agent)

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
