"""Finite-queue admission fairness fixture using real sleep Worker processes.

This is not a throughput benchmark or LLM preemption experiment. A staggered
stream of short jobs can keep a two-core request waiting under pure fitting;
aging_fit eventually stops backfilling and admits it after existing jobs finish.
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runtime.runtime import run


def fixture():
    def task(key, cpu, seconds):
        return {'id':key, 'agent':key, 'action':'sleep','seconds':seconds,'timeout':10,
                'resources':{'cpu_milli':cpu,'memory_bytes':64*1024**2,'pids':16}}
    # The anchor is deliberately staggered relative to 400ms short tasks, so
    # the two admission lanes do not normally become free in the same tick.
    return ([task('anchor',1000,.55), task('wide',2000,.1)] +
            [task(f'small_{index:02}',1000,.4) for index in range(18)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--output',type=Path,default=Path('reports/fairness.json'))
    parser.add_argument('--cgroup-root')
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error('repeats must be between 1 and 10')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    samples=[]
    for repeat in range(args.repeats):
        policies = ('resource_fit','aging_fit') if repeat%2==0 else ('aging_fit','resource_fit')
        for policy in policies:
            ready_time=[None]
            def observe(snapshot):
                if ready_time[0] is None and any(t['id']=='wide' and t['state']=='waiting'
                                                for t in snapshot['tasks']):
                    ready_time[0]=snapshot['time']
            started=time.monotonic()
            states,events=run(fixture(),max_workers=3,policy=policy,
                budget={'cpu_milli':2000,'memory_bytes':1024**3,'pids':128},
                output_dir=args.output.parent/'fairness-runs',
                cgroup_root=args.cgroup_root,quiet=True,observer=observe)
            makespan=time.monotonic()-started
            if not all(state=='succeeded' for state in states.values()):
                raise RuntimeError(f'fixture failed: {states}')
            wide_start=next(e for e in events if e['task_id']=='wide' and e['state']=='running')
            if ready_time[0] is None:
                raise RuntimeError('runtime did not publish the initial ready telemetry')
            small_before=sum(e['state']=='running' and e['task_id'].startswith('small_')
                             and e['time']<wide_start['time'] for e in events)
            samples.append({'repeat':repeat,'policy':policy,
                'wide_wait_seconds':wide_start['time']-ready_time[0],
                'makespan_seconds':makespan,'small_tasks_started_before_wide':small_before,
                'wide_ready_time':ready_time[0],'wide_started_time':wide_start['time'],
                'workflow_id':wide_start['workflow_id']})
            print(json.dumps(samples[-1]),flush=True)
    medians={policy:{metric:statistics.median(row[metric] for row in samples if row['policy']==policy)
                     for metric in ('wide_wait_seconds','makespan_seconds','small_tasks_started_before_wide')}
             for policy in ('resource_fit','aging_fit')}
    report={'scenario':'finite staggered short sleep jobs keep bypassing a two-core request',
        'enforcement':'cgroup_v2' if args.cgroup_root else 'admission_only',
        'budget':{'cpu_milli':2000,'memory_bytes':1024**3,'pids':128},
        'workload':{'anchor_seconds':.55,'wide_seconds':.1,'wide_cpu_milli':2000,
                    'small_task_count':18,'small_seconds':.4,'small_cpu_milli':1000,
                    'max_workers':3,'aging_threshold_seconds':2.0},
        'samples':samples,'medians':medians,
        'interpretation':'Finite constructed workload only; draining can trade utilization for lower wide-task wait. No universal speedup or infinite-arrival starvation proof.'}
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('MEDIANS',json.dumps(medians),flush=True)


if __name__=='__main__':
    main()
