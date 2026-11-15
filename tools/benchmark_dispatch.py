"""Five-repeat alternating baseline/candidate comparison. No LLM requests."""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


def child(repo,workload):
    sys.path.insert(0,str(Path(repo).resolve()))
    from agent_runtime.runtime import run
    from agent_runtime.scheduling import AdmissionScheduler
    from unittest.mock import patch
    if workload=='chain':
        tasks=[{'id':f't{i}','depends_on':[f't{i-1}'] if i else [],
                'resources':{'cpu_milli':1000,'memory_bytes':64*1024**2,'pids':8}} for i in reversed(range(12))]
    else:
        waves=1 if workload=='wide' else 3;width=32 if workload=='wide' else 8;tasks=[]
        for wave in range(waves):
            for i in range(width):
                tasks.append({'id':f'w{wave}_{i}','depends_on':[f'w{wave-1}_{j}' for j in range(width)] if wave else [],
                              'resources':{'cpu_milli':500,'memory_bytes':64*1024**2,'pids':8}})
    count=0;choose=AdmissionScheduler.choose
    def counted(self,*args,**kw):
        nonlocal count
        count+=1;return choose(self,*args,**kw)
    with tempfile.TemporaryDirectory() as tmp,patch.object(AdmissionScheduler,'choose',counted):
        begin=time.monotonic();start_wall=time.time();cpu=os.times()
        states,events=run(tasks,max_workers=4,output_dir=tmp,quiet=True,
                          budget={'cpu_milli':2000,'memory_bytes':1024**3,'pids':128},
                          cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'),observer=lambda snapshot:None)
        seconds=time.monotonic()-begin;after=os.times()
        assert all(s=='succeeded' for s in states.values()),states
        completed={e['task_id']:e['time'] for e in events if e['state']=='succeeded'}
        lookup={t['id']:t for t in tasks};wait=[]
        for e in events:
            if e['state']=='running':wait.append(max(0,e['time']-max([completed[d] for d in lookup[e['task_id']]['depends_on']] or [start_wall])))
        summary=json.loads(next(Path(tmp).glob('*/summary.json')).read_text())
        return {'seconds':seconds,'coordinator_cpu_seconds':after.user+after.system-cpu.user-cpu.system,
                'decisions':count,'ready_to_start_seconds_sum':sum(wait),'succeeded':len(states),
                'stats':summary.get('scheduling_stats'),'paid_model_calls':0}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--child');parser.add_argument('--workload')
    parser.add_argument('--baseline');parser.add_argument('--candidate');parser.add_argument('--output');a=parser.parse_args()
    if a.child:print(json.dumps(child(a.child,a.workload)));return
    rows=[]
    # Warm up imports/filesystems outside the measured matrix.
    for repo in (a.baseline,a.candidate):subprocess.run([sys.executable,__file__,'--child',repo,'--workload','chain'],check=True,capture_output=True,timeout=60)
    for workload in ('chain','wide','waves'):
        for repeat in range(5):
            for version in (('baseline','candidate') if repeat%2==0 else ('candidate','baseline')):
                run=subprocess.run([sys.executable,__file__,'--child',getattr(a,version),'--workload',workload],check=True,capture_output=True,text=True,timeout=60)
                rows.append({'workload':workload,'repeat':repeat,'version':version,**json.loads(run.stdout)})
    summaries=[]
    for workload in ('chain','wide','waves'):
        values={v:[r for r in rows if r['workload']==workload and r['version']==v] for v in ('baseline','candidate')}
        summary={'workload':workload}
        for version,group in values.items():
            summary[version]={key:statistics.median(r[key] for r in group) for key in ('seconds','coordinator_cpu_seconds','decisions','ready_to_start_seconds_sum')}
            summary[version]['range_seconds']=[min(r['seconds'] for r in group),max(r['seconds'] for r in group)]
        summary['median_elapsed_change_percent']=(summary['candidate']['seconds']/summary['baseline']['seconds']-1)*100
        summaries.append(summary)
    report={'summaries':summaries,'rows':rows,'budget':{'cpu_milli':2000,'memory_bytes':1024**3,'pids':128},
            'workers':4,'observer_enabled':True,'repeats':5,'paid_model_calls':0,
            'limits':'Controlled short local tasks only. No inference speedup or real-model task performance claim.'}
    Path(a.output).write_text(json.dumps(report,indent=2));print(json.dumps(summaries),flush=True)


if __name__=='__main__':main()
