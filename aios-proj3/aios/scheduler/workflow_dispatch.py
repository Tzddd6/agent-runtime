"""Shared, non-preemptive dispatch of ready DAG nodes across workflows."""
from dataclasses import dataclass, replace
from functools import lru_cache
from math import isfinite
from threading import Condition
from time import monotonic

from aios.scheduler.drf_admission import ResourceVector


POLICIES = ('fifo', 'drf', 'critical_path')


def resource_request(task):
    resources = task.get('resources') or {}
    vector = ResourceVector(cpu_milli=int(resources.get('cpu_milli', 250)),
                            memory_bytes=int(resources.get('memory_bytes', 64 * 1024**2)),
                            pids=int(resources.get('pids', 4)))
    if min(vector.cpu_milli, vector.memory_bytes, vector.pids) < 0:
        raise ValueError('Resource requests must be nonnegative')
    return vector


def remaining_paths(tasks, states):
    """Estimated duration of the longest unfinished path reachable from each node."""
    remaining = {tid: [] for tid, state in states.items() if state in ('waiting', 'running')}
    costs = {}
    for tid in remaining:
        costs[tid] = float(tasks[tid].get('estimate_seconds', .050))
        if not isfinite(costs[tid]) or not 0 <= costs[tid] <= 300:
            raise ValueError('estimate_seconds must be finite and between 0 and 300')
        for dep in tasks[tid].get('depends_on', []):
            if dep in remaining:
                remaining[dep].append(tid)

    @lru_cache(maxsize=None)
    def path(tid):
        return costs[tid] + max((path(child) for child in remaining[tid]), default=0)

    return {tid: path(tid) for tid in remaining}


@dataclass(frozen=True)
class ReadyNode:
    task_id: str
    agent: str
    resources: ResourceVector
    reserve: bool
    path_seconds: float
    deadline_at: float | None
    ready_at: float = 0


@dataclass(frozen=True)
class DispatchGrant:
    node: ReadyNode
    reserved: ResourceVector | None
    evidence: dict


class WorkflowDispatchQueue:
    """Each workflow publishes readiness. Any idle slot goes to the best admissible node."""

    def __init__(self, admission, policy, *, slots=4, aging_seconds=.350):
        if policy not in POLICIES or type(slots) is not int or not 1 <= slots <= 32:
            raise ValueError('Invalid shared dispatch policy or worker capacity')
        if not isfinite(aging_seconds) or aging_seconds <= 0:
            raise ValueError('aging_seconds must be positive and finite')
        self.admission = admission
        self.policy = policy
        self.slots = slots
        self.aging_seconds = aging_seconds
        self._condition = Condition()
        self._queues = {}
        self._eligible = set()
        self._active = set()
        self.peak_active = 0

    def publish(self, workflow, nodes, can_dispatch, completed=()):
        with self._condition:
            before = self._queues.get(workflow, {})
            now = monotonic()
            refreshed = {
                node.task_id: replace(node, ready_at=before[node.task_id].ready_at if node.task_id in before else now)
                for node in nodes
            }
            self._queues[workflow] = refreshed
            changed = before != refreshed or can_dispatch != (workflow in self._eligible)
            if can_dispatch:
                self._eligible.add(workflow)
            else:
                self._eligible.discard(workflow)
            if completed:
                self._active.difference_update((workflow, tid) for tid in completed)
            if changed or completed:
                self._condition.notify_all()

    def remove(self, workflow):
        with self._condition:
            self._queues.pop(workflow, None)
            self._eligible.discard(workflow)
            self._active = {pair for pair in self._active if pair[0] != workflow}
            self._condition.notify_all()

    def _rank(self, node, now):
        if self.policy == 'fifo':
            return (node.ready_at,)
        if self.policy == 'drf':
            share = self.admission.get_or_create_agent(node.agent).dominant_share
            return (share, node.ready_at)
        if now - node.ready_at >= self.aging_seconds:
            return (0, node.ready_at)
        slack = node.deadline_at - now - node.path_seconds if node.deadline_at is not None else float('inf')
        return (1, slack, -node.path_seconds, node.ready_at)

    def claim(self, workflow, timeout=.010):
        end = monotonic() + timeout
        with self._condition:
            while True:
                now = monotonic()
                if len(self._active) < self.slots:
                    choices = (
                        (owner, node) for owner, pending in self._queues.items() if owner in self._eligible
                        for node in pending.values() if self.admission.can_admit(node.resources, node.agent)
                    )
                    best = min(choices, key=lambda pair: self._rank(pair[1], now), default=None)
                    if best and best[0] == workflow:
                        node = best[1]
                        share = self.admission.get_or_create_agent(node.agent).dominant_share
                        try:
                            reserved = self.admission.acquire(node.agent, node.resources) if node.reserve else None
                        except RuntimeError:
                            # A concurrent syscall may reserve capacity between the check and acquire.
                            pass
                        else:
                            self._active.add((workflow, node.task_id))
                            self.peak_active = max(self.peak_active, len(self._active))
                            del self._queues[workflow][node.task_id]
                            self._condition.notify_all()
                            wait = now - node.ready_at
                            slack = node.deadline_at - now - node.path_seconds if node.deadline_at is not None else None
                            reason = self.policy
                            if self.policy == 'critical_path':
                                reason = 'aging' if wait >= self.aging_seconds else 'slack'
                            return DispatchGrant(node, reserved, {
                                'policy': self.policy, 'reason': reason, 'ready_wait_ms': wait * 1000,
                                'path_ms': node.path_seconds * 1000, 'dominant_share': share,
                                'slack_ms': slack * 1000 if slack is not None else None,
                            })
                remaining = end - now
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
