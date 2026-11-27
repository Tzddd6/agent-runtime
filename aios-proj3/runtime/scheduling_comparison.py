"""Repeatable comparison of ready-task ordering in the native DAG scheduler."""
import hashlib
import json
from math import ceil
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from runtime.launch import AIOSKernelRuntime

WORKLOAD = {
    'long': {'count': 6, 'iterations': 850_000,
             'resources': {'cpu_milli': 500, 'memory_bytes': 8 * 1024**2, 'pids': 1}},
    'short': {'count': 3, 'iterations': 12_000,
              'resources': {'cpu_milli': 150, 'memory_bytes': 32 * 1024**2, 'pids': 1}},
}
WORKERS = 2
ORDER = ('fifo', 'drf', 'drf', 'fifo')


def run_trial(policy, root):
    with AIOSKernelRuntime(storage_root=str(root / 'storage')) as kernel:
        drf = kernel.scheduler.drf_controller
        workspaces = {kind: root / kind for kind in WORKLOAD}
        for workspace in workspaces.values():
            workspace.mkdir()

        def run_chunk(task, _upstream, _attempt):
            started = perf_counter()
            agent = task['agent']
            share = drf.snapshot()['agents'][agent]['dominant_share']
            data = hashlib.pbkdf2_hmac(
                'sha256', task['id'].encode(), b'aios-scheduling-v1', WORKLOAD[agent]['iterations']
            )
            (workspaces[agent] / task['id']).write_bytes(data)
            return {'id': task['id'], 'agent': agent, 'wait_ms': (started - queued) * 1000,
                    'finish_ms': (perf_counter() - queued) * 1000, 'dominant_share': share,
                    'digest': data.hex()}

        # All chunks arrive together, in long-then-short order, and share two worker slots.
        tasks = [
            {'id': f'{kind}-{index + 1}', 'agent': kind, 'handler': run_chunk,
             'resources': spec['resources']}
            for kind, spec in WORKLOAD.items()
            for index in range(spec['count'])
        ]
        queued = perf_counter()
        result = kernel.workflow_engine.run_workflow(
            'scheduling-comparison', tasks, max_workers=WORKERS, ready_policy=policy
        )
        duration_ms = (perf_counter() - queued) * 1000
        snapshot = drf.snapshot()

    timeline = sorted((row for row in result['results'].values() if 'digest' in row),
                      key=lambda row: row['wait_ms'])
    waits = [row['wait_ms'] for row in timeline if row['agent'] == 'short']
    long_finishes = sorted(row['finish_ms'] for row in timeline if row['agent'] == 'long')
    agents_after = {name: {key: snapshot['agents'][name][key] for key in ('allocated', 'active_tasks')}
                    for name in WORKLOAD}
    resources_released = not any(snapshot['used'].values()) and all(
        not agent['active_tasks'] and not any(agent['allocated'].values()) for agent in agents_after.values()
    )
    return {
        'policy': policy, 'duration_ms': duration_ms, 'timeline': timeline,
        'short_first_ms': min(waits) if waits else None,
        'short_p95_ms': waits[ceil(len(waits) * .95) - 1] if waits else None,
        'short_completed': len(waits), 'long_completed': len(long_finishes),
        'long_completion_ms': long_finishes, 'resources_released': resources_released,
        'used_after': snapshot['used'], 'agents_after': agents_after,
        'states': result['states'],
    }


def compare_scheduling():
    with TemporaryDirectory(prefix='aios-scheduling-') as path:
        trials = [run_trial(policy, Path(path) / str(index)) for index, policy in enumerate(ORDER)]
    outputs = [{row['id']: row['digest'] for row in trial['timeline']} for trial in trials]
    count = sum(spec['count'] for spec in WORKLOAD.values())
    same_outputs = len(outputs[0]) == count and all(output == outputs[0] for output in outputs)
    summaries = {}
    for policy in ('fifo', 'drf'):
        runs = [trial for trial in trials if trial['policy'] == policy]
        summaries[policy] = {}
        for key in ('short_first_ms', 'short_p95_ms', 'duration_ms'):
            values = [trial[key] for trial in runs if trial[key] is not None]
            summaries[policy][key] = median(values) if len(values) == len(runs) else None
    return {'workers': WORKERS, 'workload': WORKLOAD, 'trials': trials, 'summaries': summaries,
            'same_outputs': same_outputs,
            'verified': same_outputs and all(trial['resources_released'] for trial in trials)}


if __name__ == '__main__':
    comparison = compare_scheduling()
    print(json.dumps(comparison, indent=2))
    raise SystemExit(0 if comparison['verified'] else 1)
