"""Repeatable FIFO/DRF/critical-path comparison with interleaved native workflows."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from aios.scheduler.workflow_dispatch import POLICIES, WorkflowDispatchQueue
from aios.tool.engineering import manifest
from cerebrum.llm.apis import LLMQuery
from runtime.launch import AIOSKernelRuntime


SLOTS = 4
AGING_SECONDS = .350
ARRIVAL_STEP = .024
ORDER = (*POLICIES, *reversed(POLICIES))
PATTERN = ('long', 'mixed', 'short', 'short', 'mixed', 'short')
DEADLINES = {'short': .250, 'long': 2.500, 'mixed': 1.350}
DEFAULT_JOBS = 60


def _tasks(job_id, kind, reference):
    def answer(node, label, depends_on=()):
        return {'id': node, 'agent': job_id, 'query_type': 'llm', 'depends_on': depends_on,
                'query_data': {'model': 'mock-aios-core', 'messages': [
                    {'role': 'user', 'content': f'{label} for {job_id}'}
                ], 'max_new_tokens': 128}, 'estimate_seconds': .032}

    def tool(node, name, parameters, estimate, depends_on=()):
        return {'id': node, 'agent': job_id, 'query_type': 'tool', 'depends_on': depends_on,
                'query_data': {'tool_calls': [{'name': name, 'parameters': parameters}]},
                'estimate_seconds': estimate}

    if kind == 'short':
        return [answer('answer', 'Summarize the input label')]
    check = tool('verify', 'project.patch_check', {'project': 'json_pointer', 'files': reference},
                 .195, ('plan',) if kind == 'mixed' else ())
    if kind == 'long':
        return [check]
    report = answer('report', 'Report completion', ('index', 'verify'))
    report['query'] = LLMQuery(**report.pop('query_data'))
    return [
        tool('index', 'text.stats', {'text': 'controlled project review text'}, .075),
        answer('plan', 'Plan the validation'),
        check,
        # Use a fixed query here. Downstream execution still waits for both dependencies.
        report,
    ]


def _digest_results(result):
    normalized = {}
    for name, output in result['results'].items():
        if name == 'verify':
            if not output['success'] or not output['tests_intact']:
                raise RuntimeError('The benchmark validation workload did not pass its tests')
            output = {'source_sha256': output['source_sha256'], 'public': output['public']['passed'],
                      'heldout': output['heldout']['passed']}
        normalized[name] = output
    return sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _trial(root, policy, count):
    with AIOSKernelRuntime(storage_root=str(root / 'storage')) as kernel:
        drf = kernel.scheduler.drf_controller
        queue = WorkflowDispatchQueue(drf, policy, slots=SLOTS, aging_seconds=AGING_SECONDS)
        reference = manifest('json_pointer')['reference_fix_files']
        origin = monotonic() + .025

        def run(index):
            kind = PATTERN[index % len(PATTERN)]
            job_id = f'mixed-load-{index:03d}'
            planned = origin + index * ARRIVAL_STEP
            tasks = _tasks(job_id, kind, reference)
            sleep(max(0, planned - monotonic()))
            submitted = monotonic()
            deadline = planned + DEADLINES[kind]
            result = kernel.workflow_engine.run_workflow(
                job_id, tasks, max_workers=2, dispatch_queue=queue, deadline_at=deadline,
            )
            finished = monotonic()
            if any(state != 'succeeded' for state in result['states'].values()):
                raise RuntimeError(f'{job_id}: {result["states"]}')
            return {
                'id': job_id, 'kind': kind, 'planned_ms': (planned - origin) * 1000,
                'submitted_ms': (submitted - origin) * 1000, 'deadline_ms': (deadline - origin) * 1000,
                'finished_ms': (finished - origin) * 1000, 'latency_ms': (finished - planned) * 1000,
                'deadline_met': finished <= deadline, 'digest': _digest_results(result),
                'nodes': [{'id': row['task_id'], **row['dispatch']} for row in result['events']
                          if row['state'] == 'running'],
            }

        with ThreadPoolExecutor(max_workers=count, thread_name_prefix='workflow-trial') as pool:
            workflows = list(pool.map(run, range(count)))
        resources = drf.snapshot()['used']
        return {'policy': policy, 'workflows': workflows, 'used_after': resources, 'peak_active': queue.peak_active,
                'resources_released': not any(resources.values()) and kernel.process_service.snapshot()['active'] == 0}


def _summarize(trials, policy):
    jobs = [job for trial in trials if trial['policy'] == policy for job in trial['workflows']]
    latencies = sorted(job['latency_ms'] for job in jobs)
    long_waits = [node['ready_wait_ms'] for job in jobs if job['kind'] == 'long' for node in job['nodes']]
    return {'sample_size': len(jobs), 'workflow_p95_ms': latencies[ceil(.95 * len(latencies)) - 1],
            'deadline_met': sum(job['deadline_met'] for job in jobs),
            'max_long_wait_ms': max(long_waits),
            'by_kind': {kind: {'count': sum(job['kind'] == kind for job in jobs),
                               'deadline_met': sum(job['deadline_met'] for job in jobs if job['kind'] == kind)}
                        for kind in DEADLINES},
            'aged_dispatches': sum(node['reason'] == 'aging' for job in jobs for node in job['nodes'])}


def compare_workflows(count=DEFAULT_JOBS):
    if type(count) is not int or not len(PATTERN) <= count <= 120:
        raise ValueError('Trial size must be an integer between 6 and 120')
    with TemporaryDirectory(prefix='aios-workflow-scheduling-') as folder:
        trials = [_trial(Path(folder) / str(index), policy, count) for index, policy in enumerate(ORDER)]
    digests = [[job['digest'] for job in trial['workflows']] for trial in trials]
    same = all(output == digests[0] for output in digests)
    return {
        'jobs_per_trial': count, 'slots': SLOTS, 'aging_ms': AGING_SECONDS * 1000,
        'arrival_step_ms': ARRIVAL_STEP * 1000, 'deadlines_ms': {k: v * 1000 for k, v in DEADLINES.items()},
        'sorting': list(ORDER), 'trials': trials, 'summaries': {policy: _summarize(trials, policy) for policy in POLICIES},
        'same_outputs': same, 'verified': same and all(trial['resources_released'] for trial in trials),
    }


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs-per-trial', type=int, default=DEFAULT_JOBS)
    args = parser.parse_args()
    comparison = compare_workflows(args.jobs_per_trial)
    print(json.dumps(comparison, indent=2))
    raise SystemExit(0 if comparison['verified'] else 1)
