"""Alternate arm order across every official task; fresh coordinator per arm.

Shares one hard CNY ledger with preflight runs. Aggregates original official
SimulationRun objects; no altered task, tool, reward, or success filtering.
"""
import argparse,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--official-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--domain',choices=['airline','telecom'],default='airline')
    parser.add_argument('--budget-ledger',type=Path,required=True)
    parser.add_argument('--cny-limit',type=float,default=10)
    args=parser.parse_args()
    from tau2.runner.helpers import get_tasks
    from tau2.data_model.simulation import Results
    from loguru import logger
    logger.remove();logger.add(sys.stderr,level='ERROR')
    tasks=get_tasks(args.domain,'base')
    args.output.mkdir(parents=True,exist_ok=False,mode=0o700)
    summary={'domain':args.domain,'trials':1,'expected_per_arm':len(tasks),'completed':False,
        'order':'task0 runtime/direct, task1 direct/runtime, alternate thereafter',
        'cny_total_limit':args.cny_limit,'shards':{'runtime':[],'direct':[]},'ranking':None}
    start=time.monotonic()
    for index,task in enumerate(tasks):
        for arm in (('runtime','direct') if index%2==0 else ('direct','runtime')):
            output=args.output/'shards'/(f'{index:03d}-'+arm)
            cmd=[sys.executable,str(ROOT/'tools/benchmark_tau.py'),'--official-root',str(args.official_root),
                 '--domain',args.domain,'--arm',arm,'--task-index',str(index),'--output',str(output),
                 '--cny-limit',str(args.cny_limit),'--max-calls','3000','--budget-ledger',str(args.budget_ledger),'--run']
            output.parent.mkdir(exist_ok=True)
            with (output.parent/(output.name+'.log')).open('w') as log:
                result=subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
            path=output/'summary.json'
            report=json.loads(path.read_text()) if path.exists() else {'completed':False,'failure_type':'ProcessFailed'}
            summary['shards'][arm].append(report)
            summary['seconds']=time.monotonic()-start
            (args.output/'progress.json').write_text(json.dumps(summary,indent=2))
            print('PAIR',index,arm,'reward',report.get('passed_tasks'),'calls',report.get('calls'),
                  'session_peak_cny',report.get('session_spent_or_reserved_cny'),flush=True)
            if result.returncode or not report['completed']:
                summary['failure_type']=report.get('failure_type')
                (args.output/'summary.json').write_text(json.dumps(summary,indent=2))
                raise SystemExit('Incomplete paired run; no submission material prepared')
    for arm in ('runtime','direct'):
        all_results=[Results.load(args.output/'shards'/(f'{i:03d}-'+arm)/'results.json') for i in range(len(tasks))]
        combined=Results(info=all_results[0].info,tasks=tasks,
            simulations=[simulation for r in all_results for simulation in r.simulations])
        assert len(combined.simulations)==len(tasks) and {r.task_id for r in combined.simulations}=={t.id for t in tasks}
        combined.save(args.output/(arm+'-results.json'))
        from tau2.metrics.agent_metrics import compute_metrics
        metrics=compute_metrics(combined)
        (args.output/(arm+'-metrics.json')).write_text(metrics.model_dump_json(indent=2))
    summary['completed']=True
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2))
    print('PAIRED_COMPLETE',len(tasks),'tasks per arm',flush=True)


if __name__=='__main__':main()
