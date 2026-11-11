"""Fixed real-model output replay: JSON-cache ablation plus real project tests.

No provider is invoked and billed_model_calls is always zero. This isolates
application context reads; it does not simulate fresh model reasoning or KV cache.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_runtime.context import ContextStore


def replay(contexts,patch_raw,output_root,*,rounds=8,repeats=3,checker=None):
    if not isinstance(contexts,list) or not 1<=len(contexts)<=32:
        raise ValueError('contexts must be a list of 1..32 JSON objects')
    if type(rounds) is not int or not 1<=rounds<=100 or type(repeats) is not int or not 1<=repeats<=10:
        raise ValueError('rounds/repeats out of bounds')
    from agent_runtime.engineering import validate_patch,run_check
    source=validate_patch(patch_raw)
    checker=run_check if checker is None else checker
    output_root=Path(output_root).resolve()
    output_root.mkdir(parents=True,exist_ok=True)
    roles=('analyzer','tester','repairer','reviewer')
    rows=[]
    for repeat in range(repeats):
        for mode in (('disabled','lru2') if repeat%2==0 else ('lru2','disabled')):
            row_started=time.perf_counter()
            directory=output_root/f'{repeat}-{mode}'
            directory.mkdir(exist_ok=False)
            store=ContextStore(directory/'contexts',cache_bytes=0 if mode=='disabled' else 8*1024**2)
            refs=[store.put(context,roles) for context in contexts]
            start=time.perf_counter();cpu=time.process_time();accesses=0
            for _ in range(rounds):
                for role in roles:
                    for ref in refs:
                        store.get(ref,role);accesses+=1
            cache_seconds=time.perf_counter()-start
            cache_cpu=time.process_time()-cpu
            start=time.perf_counter()
            tests=checker(source,directory/'project-check')
            test_seconds=time.perf_counter()-start
            row={'id':f'cache-{repeat}-{mode}','kind':'fixed_model_output_replay','repeat':repeat,
                 'cache_mode':mode,'model_calls':0,'billed_model_calls':0,'usage_source':'no provider calls',
                 'source_sha256':hashlib.sha256(source.encode()).hexdigest(),
                 'context_seconds':cache_seconds,'context_controller_cpu_seconds':cache_cpu,
                 'test_seconds':test_seconds,'wall_seconds':time.perf_counter()-row_started,
                 'success':bool(tests.get('success')),'accesses':accesses,'stats':store.stats(),'test_results':tests,
                 'usage':{},'cpu_seconds':None,'peak_memory_bytes':None}
            rows.append(row)
    report={'kind':'fixed_model_output_replay','rounds':rounds,'repeats':repeats,'logical_readers':list(roles),
            'records':rows,'billed_model_calls':0,
            'medians':{mode:{key:statistics.median(row[key] if key=='context_seconds' else row['stats'][key]
                        for row in rows if row['cache_mode']==mode)
                        for key in ('context_seconds','disk_reads','cache_hits','stored_bytes')}
                        for mode in ('disabled','lru2')},
            'claim_boundary':'Fixed model output replay, sequential logical-reader accesses, real project tests. '
                'No fresh model responses, no paid calls, no GPU KV-cache measurement. File reads are not physical disk I/O. '
                'Context CPU excludes subprocess test CPU; timings do not establish statistical significance.'}
    (output_root/'cache-replay.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contexts',type=Path,required=True,help='JSON list of actual project/model-result contexts')
    parser.add_argument('--patch',type=Path,required=True,help='Saved model JSON response accepted by validate_patch')
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=8)
    parser.add_argument('--repeats',type=int,default=3)
    args=parser.parse_args()
    report=replay(json.loads(args.contexts.read_text(encoding='utf-8-sig')),
                  args.patch.read_text(encoding='utf-8-sig'),args.output_dir,rounds=args.rounds,repeats=args.repeats)
    print(json.dumps({'medians':report['medians'],'report':str(args.output_dir/'cache-replay.json'),
                      'billed_model_calls':0},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
