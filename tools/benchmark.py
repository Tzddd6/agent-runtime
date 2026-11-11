"""Reproducible synthetic scheduling experiment; results are local to this fixture."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runtime.runtime import run


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repeats', type=int, default=3)
    p.add_argument('--output', type=Path, default=Path('reports/benchmark.json'))
    p.add_argument('--cgroup-root')
    args = p.parse_args()
    if not 1 <= args.repeats <= 20:
        p.error('repeats must be between 1 and 20')
    tasks = [
        {'id':'anchor', 'action':'sleep', 'seconds':.5, 'resources':{'cpu_milli':1000}},
        {'id':'wide', 'action':'sleep', 'seconds':.3, 'resources':{'cpu_milli':2000}},
        {'id':'small', 'action':'sleep', 'seconds':.1, 'resources':{'cpu_milli':1000}}]
    rows = []
    for repeat in range(args.repeats):
        # Alternate order to reduce fixed warm-up bias.
        for policy in (('fifo','resource_fit') if repeat % 2 == 0 else ('resource_fit','fifo')):
            started = time.monotonic()
            states, events = run(tasks, policy=policy, max_workers=3,
                budget={'cpu_milli':2000, 'memory_bytes':1024**3, 'pids':128},
                output_dir=args.output.parent/'benchmark-runs', cgroup_root=args.cgroup_root, quiet=True)
            assert all(v=='succeeded' for v in states.values()), states
            start = events[0]['time']
            small = next(e['time']-start for e in events if e['task_id']=='small' and e['state']=='succeeded')
            rows.append({'repeat':repeat, 'policy':policy, 'wall_seconds':time.monotonic()-started,
                         'small_completion_seconds':small, 'workflow_id':events[0]['workflow_id']})
    result = {'scenario':'synthetic head-of-line blocking with sleep workers',
              'enforcement':'cgroup_v2' if args.cgroup_root else 'admission_only',
              'samples':rows, 'medians':{policy:{metric:statistics.median(
                  r[metric] for r in rows if r['policy']==policy)
                  for metric in ('wall_seconds','small_completion_seconds')}
                  for policy in ('fifo','resource_fit')}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result['medians'], indent=2))


if __name__ == '__main__':
    main()
