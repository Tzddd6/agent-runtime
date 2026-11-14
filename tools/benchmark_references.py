"""Alternating real-process transport comparison against a specified older clone.

No model calls. Counts launch payload bytes, not all IPC traffic or memory copies.
Run inside a CPU/memory-capped delegated service on the target OS.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def child(repo,kib):
    sys.path.insert(0,str(Path(repo).resolve()))
    from agent_runtime.runtime import run
    from agent_runtime.executor import Executor
    from unittest.mock import patch
    import os
    text=('bounded shared context. '*12000)[:kib*1024]
    tasks=[{'id':'source','message':{'text':text}}]+[
        {'id':f'reader{i}','action':'review_document','depends_on':['source'],
         'inputs':['source'],'contexts':[{'text':text}]} for i in range(4)]
    sizes={};launch=Executor.launch
    def capture(executor,payload,*args,**kwargs):
        sizes[payload['id']]=len(json.dumps(payload,ensure_ascii=False).encode())
        return launch(executor,payload,*args,**kwargs)
    with tempfile.TemporaryDirectory() as tmp,patch.object(Executor,'launch',capture):
        start=time.monotonic()
        states,events=run(tasks,output_dir=tmp,quiet=True,
                          cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
        elapsed=time.monotonic()-start
        assert all(s=='succeeded' for s in states.values()),states
        results=[e['result']['result'] for e in events
                 if e['state']=='succeeded' and e['task_id'].startswith('reader')]
        assert len(results)==4 and all(r['report']=={'source':{'text':text}} for r in results)
        summary=json.loads(next(Path(tmp).glob('*/summary.json')).read_text())
        return {'seconds':elapsed,'consumer_payload_bytes':sum(v for k,v in sizes.items() if k!='source'),
                'successful_consumers':len(results),'context_stats':summary['context_stats'],
                'dataflow':summary.get('dataflow'),'paid_calls':0}


def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline');p.add_argument('--candidate')
    p.add_argument('--output');p.add_argument('--child');p.add_argument('--kib',type=int,default=96);args=p.parse_args()
    if args.child:
        print(json.dumps(child(args.child,args.kib)));return
    rows=[]
    for kib in (4,96,256):
        for repeat in range(3):
            order=('baseline','candidate') if repeat%2==0 else ('candidate','baseline')
            for mode in order:
                process=subprocess.run([sys.executable,__file__,'--child',getattr(args,mode),'--kib',str(kib)],
                                       text=True,capture_output=True,timeout=45,check=True)
                rows.append({'context_kib':kib,'repeat':repeat,'mode':mode,**json.loads(process.stdout)})
    report={'rows':rows,'scope':'4 consumers, 4/96/256 KiB identical inputs, alternating order, three repeats',
            'limits':'Payload bytes exclude Broker GET traffic, events and result files. No zero-copy or total speedup claim.'}
    Path(args.output).write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
