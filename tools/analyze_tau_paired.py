"""Summarize complete paired official trajectories without hiding failed tasks."""
import argparse,json,math,statistics
from pathlib import Path


def wilson(success,n):
    z=1.96;p=success/n;den=1+z*z/n
    mid=(p+z*z/(2*n))/den
    radius=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [mid-radius,mid+radius]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('run_dir',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();summary=json.loads((args.run_dir/'summary.json').read_text())
    assert summary['completed'],'Do not turn partial runs into a complete benchmark'
    arms={};n=summary['expected_per_arm'];rows=[]
    for arm in ('runtime','direct'):
        reports=summary['shards'][arm];assert len(reports)==n and all(r['completed'] for r in reports)
        success=sum(r['passed_tasks'] for r in reports)
        roles={}
        for index in range(n):
            path=args.run_dir/'shards'/(f'{index:03d}-'+arm)/'attempts/calls.jsonl'
            for line in path.read_text().splitlines():
                call=json.loads(line);actor=call['actor'];group=roles.setdefault(actor,{'calls':0,'prompt_tokens':0,'completion_tokens':0,'cache_hit_tokens':0,'peak_cost_cny':0,'turn_seconds':0})
                group['calls']+=1
                group['turn_seconds']+=call['seconds']
                for key in ('prompt_tokens','completion_tokens'):group[key]+=call.get('usage',{}).get(key,0)
                group['cache_hit_tokens']+=call.get('usage',{}).get('prompt_cache_hit_tokens',0)
                group['peak_cost_cny']+=call.get('conservative_peak_cost_cny') or 0
        arms[arm]={'success':success,'tasks':n,'pass_1':success/n,'wilson95':wilson(success,n),
            'roles':roles,'task_seconds_median':statistics.median(r['seconds'] for r in reports),
            'task_seconds_sum':sum(r['seconds'] for r in reports),
            'coordinator_cpu_seconds':sum(r['coordinator_cpu_seconds'] for r in reports),
            'children_cpu_seconds':sum(r['children_cpu_seconds'] for r in reports),
            'coordinator_maxrss_kib_max':max(r['coordinator_maxrss_kib'] for r in reports),
            'children_maxrss_kib_max':max(r['children_maxrss_kib'] for r in reports),
            'peak_cost_cny':sum(r['known_peak_cost_cny'] for r in reports)}
    b=c=0;deltas=[]
    for index in range(n):
        r=summary['shards']['runtime'][index];d=summary['shards']['direct'][index]
        if r['passed_tasks'] and not d['passed_tasks']:b+=1
        if d['passed_tasks'] and not r['passed_tasks']:c+=1
        deltas.append(r['seconds']-d['seconds'])
        rows.append({'task_index':index,'runtime_success':r['passed_tasks'],'direct_success':d['passed_tasks'],
                     'runtime_seconds':r['seconds'],'direct_seconds':d['seconds']})
    discordant=b+c
    p=min(1.,2*sum(math.comb(discordant,i) for i in range(min(b,c)+1))/2**discordant) if discordant else 1.
    report={'benchmark':'tau2 v1.0.1 airline base, ALL evaluator','upstream':'fc0055dc4e0a316c3f83133267fbd6faaa770992',
        'runtime_source':'cf4a11a0ed65d95b83b86971d1924093b007d554','trials_per_task':1,'arms':arms,
        'paired':{'runtime_only_success':b,'direct_only_success':c,'mcnemar_exact_p_two_sided':p,
                  'median_task_seconds_delta_runtime_minus_direct':statistics.median(deltas)},
        'caveats':['Single trial per task, no pass^2/3/4 claim','DeepSeek simulator differs from default GPT-4.1; custom configuration',
            'API sampling and adaptive tool paths can differ despite equal settings; not a pure causal framework-speed experiment',
            'CPU and RSS cover local coordinators/workers; external inference and key broker excluded; coordinator CPU and task seconds exclude initial imports',
            'CNY expenses use peak list rates as a conservative upper estimate, not the actual invoice'],
        'ranking':None,'official_review_status':'not_submitted','tasks':rows}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!='tasks'},indent=2))


if __name__=='__main__':main()
