"""Compare disabled, LRU and LRU-2 JSON caches on a repeated-hot plus scan trace."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_runtime.context import ContextStore


def experiment(root,mode,rounds):
    store=ContextStore(root,cache_bytes=0 if mode=='disabled' else 512*1024,
                       cache_max_entries=3,cache_policy='lru' if mode=='lru' else 'lru2')
    hot=[store.put({'id':i,'text':'shared project facts '*6000},['a']) for i in range(2)]
    cold=[store.put({'id':i+2,'text':'single pass evidence '*1500},['a']) for i in range(16)]
    accesses=0
    started=time.perf_counter()
    for _ in range(rounds):
        # Initial double access makes the repeatedly used items distinguishable.
        trace=[hot[0],hot[0],hot[1],hot[1],*cold,*([hot[0],hot[1]]*4)]
        for ref in trace:
            value=store.get(ref,'a')
            assert 'text' in value
            accesses+=1
    return {'mode':mode,'elapsed_seconds':time.perf_counter()-started,
            'accesses':accesses,'stats':store.stats()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds',type=int,default=8)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--output',type=Path,default=Path('reports/context-benchmark.json'))
    args=parser.parse_args()
    if not 1<=args.rounds<=100 or not 1<=args.repeats<=10:
        parser.error('rounds must be 1..100 and repeats 1..10')
    samples=[]
    with tempfile.TemporaryDirectory(prefix='agentrt-context-bench-') as temporary:
        for repeat in range(args.repeats):
            modes=('disabled','lru','lru2') if repeat%2==0 else ('lru2','lru','disabled')
            for mode in modes:
                row=experiment(Path(temporary)/f'{repeat}-{mode}',mode,args.rounds)
                row['repeat']=repeat
                samples.append(row)
    medians={mode:{metric:statistics.median(
        row[metric] if metric=='elapsed_seconds' else row['stats'][metric]
        for row in samples if row['mode']==mode)
        for metric in ('elapsed_seconds','disk_reads','disk_read_bytes','cache_hits','cache_evictions')}
        for mode in ('disabled','lru','lru2')}
    report={'scenario':'2 repeated shared contexts plus 16 one-pass context objects per round',
            'caveat':'Application JSON byte cache; file read calls are not physical device IO. '
                     'OS page cache remains enabled. No model inference or GPU KV cache is measured.',
            'rounds':args.rounds,'repeats':args.repeats,'samples':samples,'medians':medians}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(medians,indent=2))


if __name__=='__main__':main()
