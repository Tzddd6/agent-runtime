"""Alternated, zero-provider-call official replay of baseline/candidate/direct.

Run inside an explicitly delegated, private-network systemd validation unit.
Task/tool/grader and fixed replies are unchanged; this is not a new score.
"""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path


def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True);p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--trajectory',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--limit',type=int)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    from agent_runtime.web import delegated_attempts
    group=delegated_attempts();rows=[]
    for repetition,order in enumerate([['baseline','candidate','direct'],['direct','candidate','baseline']]):
        for arm in order:
            root=args.baseline if arm=='baseline' else args.candidate
            folder=args.output/f'{repetition}-{arm}'
            command=[sys.executable,str(root/'tools/replay_tau_framework.py'),'--trajectory',str(args.trajectory),'--output',str(folder)]
            command+=['--direct'] if arm=='direct' else ['--session']
            if args.limit:command+=['--limit',str(args.limit)]
            with (args.output/f'{repetition}-{arm}.log').open('w') as log:
                process=subprocess.run(command,cwd=root,env=dict(os.environ,PYTHONPATH=str(root),AGENTRT_TEST_CGROUP=group,LITELLM_LOCAL_MODEL_COST_MAP='True'),stdout=log,stderr=subprocess.STDOUT)
            if process.returncode:raise RuntimeError(f'Replay failed in {repetition}/{arm}; failed logs retained')
            value=json.loads((folder/'summary.json').read_text());value.update(arm=arm,repetition=repetition)
            rows.append(value);print(json.dumps({k:value[k] for k in ('arm','repetition','cases','seconds','all_identical','supplier_requests')}),flush=True)
            assert not [p for p in Path(group).iterdir() if p.is_dir()]
    reference=rows[0]
    assert all(r['request_sequence_sha256']==reference['request_sequence_sha256'] and r['rewards']==reference['rewards'] and r['all_identical'] for r in rows)
    arms={arm:{'seconds_mean':sum(r['seconds'] for r in rows if r['arm']==arm)/2,
               'seconds':[r['seconds'] for r in rows if r['arm']==arm],
               'coordinator_cpu_mean':sum(r['coordinator_cpu_seconds'] for r in rows if r['arm']==arm)/2,
               'children_cpu_mean':sum(r['children_cpu_seconds'] for r in rows if r['arm']==arm)/2} for arm in ('baseline','candidate','direct')}
    report={'arms':arms,'cases_per_arm_per_repetition':reference['cases'],'repetitions':2,'all_requests_dialogue_rewards_identical':True,
            'supplier_requests':0,'network':'private loopback only','rows':rows,
            'candidate_elapsed_change_percent':100*(arms['candidate']['seconds_mean']/arms['baseline']['seconds_mean']-1),
            'scope':'Runtime without optional durable checkpoint; not a live model comparison or new leaderboard score',
            'limits':['Two repetitions, not a statistically established universal speedup','Token content fixed; no token-saving or model-quality improvement claim','RSS values are per-process high-water marks, not aggregate peak memory']}
    (args.output/'summary.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)


if __name__=='__main__':main()
