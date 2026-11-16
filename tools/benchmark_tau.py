"""Pinned official tau2 tasks and unchanged ALL evaluator, measured custom arm.

Paid calls require explicit --run, CNY limit and a shared persistent budget
ledger. No supplier key is read. Call errors stop the run without a replay.
Only full domain coverage can be marked submission-ready.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import resource
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

UPSTREAM='fc0055dc4e0a316c3f83133267fbd6faaa770992'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--official-root',type=Path,required=True)
    parser.add_argument('--domain',choices=['airline','telecom','retail','banking_knowledge'],default='airline')
    parser.add_argument('--arm',choices=['runtime','direct'],default='runtime')
    parser.add_argument('--pilot',type=int,default=0)
    parser.add_argument('--task-index',type=int)
    parser.add_argument('--trials',type=int,default=1)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cny-limit',type=float,default=10.)
    parser.add_argument('--max-calls',type=int,default=1000)
    parser.add_argument('--budget-ledger',type=Path)
    parser.add_argument('--run',action='store_true')
    args=parser.parse_args()
    from tau2.runner.helpers import get_tasks,get_info
    from tau2.runner.batch import run_single_task
    from tau2.evaluator.evaluator import EvaluationType
    from tau2.data_model.simulation import TextRunConfig,Results
    from tau2.data_model.tasks import RewardType
    from loguru import logger
    logger.remove();logger.add(sys.stderr,level='ERROR')
    manifest=json.loads((args.official_root/'SOURCE_MANIFEST.json').read_text())
    assert manifest['commit']==UPSTREAM
    # Verify the executable environment, evaluator, tools, policies, tasks and
    # simulator inputs, not only a tag string. Excluded historical answers unused.
    verified=0
    for name,digest in manifest['sha256'].items():
        if name.startswith(('src/','data/tau2/domains/','data/tau2/user_simulator/')):
            if hashlib.sha256((args.official_root/name).read_bytes()).hexdigest()!=digest:
                raise SystemExit('Official runtime file changed: '+name)
            verified+=1
    tasks=get_tasks(args.domain,'base')
    if args.task_index is not None and not 0<=args.task_index<len(tasks):raise SystemExit('Invalid task shard')
    requires_judge=any(t.evaluation_criteria and RewardType.NL_ASSERTION in t.evaluation_criteria.reward_basis for t in tasks)
    plan={'upstream':UPSTREAM,'domain':args.domain,'domain_tasks':len(tasks),'trials':args.trials,
          'selected_tasks':1 if args.task_index is not None else min(args.pilot,len(tasks)) if args.pilot else len(tasks),
          'task_index':args.task_index,
          'pilot':bool(args.pilot),'official_files_verified':verified,
          'requires_official_gpt41_judge':requires_judge,'limit_cny':args.cny_limit,
          'arm':args.arm,'submission_type':'custom','user_simulator_model':'deepseek-flash',
          'evaluator':'official ALL, unchanged','ranking':None}
    print(json.dumps(plan),flush=True)
    if not args.run:return
    if args.pilot<0 or args.trials<1:raise SystemExit('Invalid run size')
    if requires_judge:raise SystemExit('Official GPT-4.1 judge unavailable: refusing silent grader replacement')
    if args.budget_ledger is None:raise SystemExit('A shared persistent budget ledger is required')
    args.output.mkdir(parents=True,exist_ok=False,mode=0o700)
    from agent_runtime.model_broker import BrokerClient
    from agent_runtime.tau_adapter import TauBridge
    bridge=TauBridge(BrokerClient.from_env(),args.output/'attempts',arm=args.arm,
        cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'),cny_limit=args.cny_limit,
        max_calls=args.max_calls,ledger=args.budget_ledger)
    bridge.install()
    config=TextRunConfig(domain=args.domain,task_split_name='base',llm_agent='deepseek-flash',llm_user='deepseek-flash',
        llm_args_agent={'temperature':0.,'max_tokens':2048},llm_args_user={'temperature':0.,'max_tokens':2048},
        num_trials=args.trials,max_concurrency=1,max_retries=0,hallucination_retries=0,auto_resume=False,
        auto_review=False,seed=300,max_steps=200)
    selected=[tasks[args.task_index]] if args.task_index is not None else tasks[:args.pilot] if args.pilot else tasks
    results=Results(info=get_info(config),tasks=selected,simulations=[])
    (args.output/'protocol.json').write_text(json.dumps({**plan,'config':config.model_dump(mode='json'),
        'methodology':'Native tool schemas and messages preserved; DeepSeek simulator explicitly replaces default GPT-4.1. Agent inference uses Runtime Attempts; official tools execute inside official coordinator under its service cgroup. No cross-task memory. No uncertainty retries.'},indent=2))
    failure=None;start=time.monotonic();cpu_start=time.process_time()
    try:
        for trial in range(args.trials):
            for task in selected:
                print('START',args.arm,args.domain,task.id,trial,flush=True)
                result=run_single_task(config,task,seed=300+trial,evaluation_type=EvaluationType.ALL,save_dir=args.output/'official-logs')
                result.trial=trial;result.seed=300+trial
                results.simulations.append(result)
                results.save(args.output/'results.json')
                if bridge.budget_exhausted:raise RuntimeError('Budget exhausted; evaluation stopped')
                if any(r['status']!='succeeded' for r in bridge.records):raise RuntimeError('Transport/protocol failure; evaluation stopped without replay')
                print('DONE',task.id,result.reward_info.reward,result.termination_reason,flush=True)
    except Exception as exc:
        failure=type(exc).__name__
        results.save(args.output/'results.json')
    summary={**plan,'finished':len(results.simulations),'expected':len(selected)*args.trials,
        'completed':failure is None and len(results.simulations)==len(selected)*args.trials,
        'passed_tasks':sum(r.reward_info.reward==1 for r in results.simulations),
        'seconds':time.monotonic()-start,'failure_type':failure,
        'calls':len(bridge.records),'usage':{k:sum(r.get('usage',{}).get(k,0) for r in bridge.records) for k in ('prompt_tokens','completion_tokens','prompt_cache_hit_tokens')},
        'known_peak_cost_cny':sum(r.get('conservative_peak_cost_cny') or 0 for r in bridge.records),
        'session_spent_or_reserved_cny':bridge.budget.spent,'submission_ready':False,
        'coordinator_cpu_seconds':time.process_time()-cpu_start,
        'coordinator_maxrss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'children_maxrss_kib':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        'children_cpu_seconds':resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime+resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime,
        'provider_resources':'External provider and RAM-key broker excluded; only local coordinator and Runtime workers measured'}
    summary['full_coverage']=summary['completed'] and not args.pilot and args.task_index is None
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps(summary),flush=True)
    raise SystemExit(0 if summary['completed'] else 1)


if __name__=='__main__':main()
