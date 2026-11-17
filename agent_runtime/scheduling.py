"""Non-preemptive resource admission with an optional bounded-bypass policy.

AIOS motivates centralized scheduling and FIFO baselines. ``aging_fit`` is this
project's own composition: resource fitting plus old-request drain reservation.
It is not AIOS RR, CFS, or an LLM generation preemption implementation.
"""
from dataclasses import dataclass
import math
import time


@dataclass(frozen=True)
class Decision:
    task_id: str = None
    reason: str = 'empty'
    draining_task_id: str = None
    waited_seconds: float = 0.0


class AdmissionScheduler:
    POLICIES = ('fifo', 'resource_fit', 'aging_fit')

    def __init__(self, policy, budget, defaults, aging_seconds=2.0):
        if policy not in self.POLICIES:
            raise ValueError('unknown scheduling policy')
        if (isinstance(aging_seconds, bool) or not isinstance(aging_seconds, (float, int))
                or not math.isfinite(aging_seconds) or aging_seconds <= 0):
            raise ValueError('aging_seconds must be a finite positive number')
        if (not budget or set(budget) != set(defaults)
                or any(type(v) is not int or v <= 0 for v in [*budget.values(), *defaults.values()])):
            raise ValueError('budget and defaults must have matching positive integer resources')
        self.policy = policy
        self.budget, self.defaults = dict(budget), dict(defaults)
        self.aging_seconds = float(aging_seconds)
        self.ready_since = {}
        self.sequence = {}
        self._next_sequence = 0
        self.last_decision = Decision()

    def started(self, task_id):
        """Forget one admission's ready age after the caller launches it.

        Explicit notification avoids carrying age into an immediate retry when a
        very short Attempt starts and exits between two calls to ``choose``.
        Calling this for an unknown/already removed ID is harmless.
        """
        self.ready_since.pop(task_id, None)
        self.sequence.pop(task_id, None)

    def choose(self, ready_tasks, used, *, busy_agents=(), slots_available=1, now=None):
        """Choose one ready task; the caller still owns launch and resource accounting.

        Call again after each successful launch with the new ``used`` vector and
        remaining ready tasks, and call ``started(id)`` on successful launches.
        Pass the *whole* dependency-ready set, including
        busy-agent tasks; only absent IDs lose their waiting age. No deadline or
        starvation bound is claimed if a running attempt never releases resources.
        """
        if (set(used) != set(self.budget)
                or any(type(v) is not int or v < 0 or v > self.budget[k] for k,v in used.items())
                or type(slots_available) is not int or slots_available < 0):
            raise ValueError('invalid reservation vector or slot count')
        now = time.monotonic() if now is None else now
        if isinstance(now, bool) or not isinstance(now, (float, int)) or not math.isfinite(now):
            raise ValueError('now must be a finite monotonic timestamp')
        ready = list(ready_tasks)
        identifiers = [t['id'] for t in ready]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError('ready task IDs must be unique')
        current = set(identifiers)
        self.ready_since = {key:stamp for key,stamp in self.ready_since.items() if key in current}
        self.sequence = {key:sequence for key,sequence in self.sequence.items() if key in current}
        limits = {}
        for task in ready:
            key = task['id']
            request = {**self.defaults, **task.get('resources', {})}
            if set(request) != set(self.budget) or any(type(v) is not int or v <= 0 for v in request.values()):
                raise ValueError('invalid task resource request')
            limits[key] = request
            if key not in self.ready_since:
                self.ready_since[key] = now
                self.sequence[key] = self._next_sequence
                self._next_sequence += 1

        busy = set(busy_agents)
        def fits(task, available=False):
            return all(limits[task['id']][k] + (used[k] if available else 0) <= self.budget[k]
                       for k in self.budget)

        def decision(task=None, reason='empty', draining=None):
            value = Decision(task['id'] if task else None, reason, draining,
                             max(0.0, now-self.ready_since[task['id']]) if task else
                             max(0.0, now-self.ready_since[draining]) if draining else 0.0)
            self.last_decision = value
            return value

        # Impossible requests must be marked unschedulable by the caller; they
        # cannot reserve/drain the queue and prevent independent tasks progressing.
        feasible = [t for t in ready if fits(t)]
        if not feasible:
            return decision()
        if not slots_available:
            return decision(reason='worker_slots_full')

        if self.policy == 'fifo':
            first = feasible[0]
            if first.get('agent',first['id']) in busy:
                return decision(reason='agent_busy')
            if not fits(first, available=True):
                return decision(reason='fifo_head_blocked')
            return decision(first, 'fifo')

        eligible = [t for t in feasible if t.get('agent',t['id']) not in busy]
        if self.policy == 'aging_fit':
            aged = [t for t in eligible if now-self.ready_since[t['id']] >= self.aging_seconds]
            if aged:
                oldest = min(aged, key=lambda t:(self.ready_since[t['id']],self.sequence[t['id']]))
                if fits(oldest, available=True):
                    return decision(oldest, 'aged_admission')
                # Do not kill/preempt the current Worker. Stop backfilling until
                # its finite execution/timeout/cleanup releases enough resources.
                return decision(reason='aging_drain', draining=oldest['id'])

        for task in eligible:
            if fits(task, available=True):
                return decision(task, 'resource_fit')
        return decision(reason='resources_unavailable' if eligible else 'agent_busy')
