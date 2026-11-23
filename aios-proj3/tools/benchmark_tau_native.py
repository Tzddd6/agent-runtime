"""Pinned tau2 paired evaluation through the native AIOS Kernel, no key export."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time

UPSTREAM='fc0055dc4e0a316c3f83133267fbd6faaa770992'

def atomic(path,value):
    temporary=path.with_suffix('.tmp')
    with temporary.open('w') as f:
        json.dump(value,f,indent=2); f.flush();os.fsync(f.fileno())
    os.replace(temporary,path)

class BrokerProvider:
    def __init__(self,client): self.client=client
    def complete(self,query):
        from agent_runtime.benchmark_transport import encode
        from cerebrum.llm.apis import LLMResponse
        wire=encode(query.messages,query.tools,temperature=query.temperature,
                    seed=query.seed,tool_choice=query.tool_choice)
        result=self.client.complete(wire,query.max_new_tokens)
        if result.get('finish_reason') not in ('stop','tool_calls'):
            raise RuntimeError('Incomplete provider output; no replay')
        msg=json.loads(result['content'])['message']
        return LLMResponse(response_message=msg.get('content') or '',
                           tool_calls=msg.get('tool_calls'),usage=result.get('usage') or {})

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--native-root',type=Path,required=True)
    p.add_argument('--official-root',type=Path,required=True)
    p.add_argument('--legacy-root',type=Path,required=True)
    p.add_argument('--ledger',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--tasks',type=int,default=2)
    p.add_argument('--trials',type=int,default=1)
    p.add_argument('--seed',type=int,default=400)
    p.add_argument('--run',action='store_true')
    args=p.parse_args()
    if args.tasks not in range(1,51) or not 1<=args.trials<=4: raise ValueError('Invalid evaluation size')
    os.environ['LITELLM_LOCAL_MODEL_COST_MAP']='True'
    os.environ['AGENTRT_PROVIDER_STREAMING']='0'
    sys.path[:0]=[str(args.native_root),str(args.legacy_root),str(args.official_root/'src')]
    from tau2.runner.helpers import get_tasks,get_info
    from tau2.data_model.simulation import TextRunConfig,Results
    from tau2.data_model.tasks import RewardType
    from tau2.runner.batch import run_single_task
    from tau2.evaluator.evaluator import EvaluationType
    from tau2.data_model.message import SystemMessage,UserMessage,ToolMessage,AssistantMessage,ToolCall
    from aios.benchmarks.tau_bridge import NativeTauTransport
    from aios.benchmarks.progress import status_print
    global print
    print = status_print
    from runtime.launch import AIOSKernelRuntime
    from agent_runtime.tau_adapter import EvaluationBudget
    from agent_runtime.model_broker import BrokerClient
    from loguru import logger
    logger.remove();logger.add(sys.stderr,level='ERROR')
    manifest=json.loads((args.official_root/'SOURCE_MANIFEST.json').read_text())
    if manifest['commit']!=UPSTREAM: raise ValueError('Official commit mismatch')
    verified=0
    for name,digest in manifest['sha256'].items():
        if name.startswith(('src/','data/tau2/domains/','data/tau2/user_simulator/')):
            if hashlib.sha256((args.official_root/name).read_bytes()).hexdigest()!=digest:
                raise ValueError('Official source/data mismatch')
            verified+=1
    tasks=get_tasks('airline','base')
    if len(tasks)!=50: raise ValueError('Official task coverage mismatch')
    if any(t.evaluation_criteria and RewardType.NL_ASSERTION in t.evaluation_criteria.reward_basis for t in tasks):
        raise ValueError('GPT judge required; no automatic provider replacement')
    # Official message models/converter exercised before any paid request.
    from cerebrum.llm.apis import LLMResponse
    class Fixed:
        def complete(self,q):
            assert q.messages[-1]['tool_call_id']=='fixture1'
            return LLMResponse(response_message='fixture complete',usage={'total_tokens':1})
    fixtures=[SystemMessage(role='system',content='fixed policy'),UserMessage(role='user',content='fixture'),
              AssistantMessage(role='assistant',content=None,tool_calls=[ToolCall(id='fixture1',name='probe',arguments={})]),
              ToolMessage(role='tool',id='fixture1',content='fixed observation')]
    import tempfile
    with tempfile.TemporaryDirectory() as folder,AIOSKernelRuntime(storage_root=folder) as kernel:
        bridge=NativeTauTransport(kernel,Fixed(),arm='runtime',model='fixed',reserve=lambda q,a:0,settle=lambda r,u:0)
        try:
            assert bridge.generate('fixed',fixtures,call_name='agent_response',max_tokens=64).content=='fixture complete'
        finally:bridge.close()
    print(json.dumps({'official_files_verified':verified,'official_message_preflight':'passed','domain_tasks':len(tasks),'run':args.run}),flush=True)
    for line in Path('/home/tzd/.config/agentrt/console.env').read_text().splitlines():
        if line.startswith(('AGENTRT_PROXY_TOKEN=','AGENTRT_MODEL_BROKER_URL=')):
            k,v=line.split('=',1);os.environ[k]=v
    client=BrokerClient(os.environ['AGENTRT_PROXY_TOKEN'],'http://127.0.0.1:18888')
    profiles=client.profiles_info()['profiles']
    ready=[row for row in profiles if row.get('configured') and row.get('provider')=='deepseek']
    if len(ready)!=1:
        raise RuntimeError('Exactly one configured DeepSeek evaluation profile required')
    profile=ready[0]
    if profile['model']=='ds':
        client.save_profile({'id':profile['id'],'label':profile['label'],
                             'provider':'deepseek','model':'deepseek-flash'})
        profile=next(row for row in client.profiles_info()['profiles'] if row['id']==profile['id'])
    if profile['model']!='deepseek-flash':
        raise RuntimeError('Required frozen model is DeepSeek Flash')
    client=client.for_profile(profile['id'],profile['model'])
    print(json.dumps({'provider_configured':True,'model':profile['model'],'paid_calls':0}),flush=True)
    if not args.run:return
    commit=subprocess.check_output(['git','-C',str(args.native_root),'rev-parse','HEAD'],stderr=subprocess.DEVNULL,text=True).strip()
    subprocess.run(['git','-C',str(args.native_root),'diff','--quiet','HEAD','--','aios','cerebrum','runtime','tools'],check=True)
    ledger=json.loads(args.ledger.read_text())
    if ledger['limit_cny']!=20:raise ValueError('Canonical shared budget mismatch')
    # Read the existing ledger before creating a new output; never reset history.
    budget=EvaluationBudget(20,10000,ledger=args.ledger)
    with budget.transaction(): pass
    args.output.mkdir(parents=True,exist_ok=False,mode=0o700)
    hashes={str(path.relative_to(args.native_root)):hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in ('aios','cerebrum','runtime') for path in (args.native_root/folder).rglob('*.py')}
    atomic(args.output/'source-receipt.json',{'native_commit':commit,'sha256':hashes,'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'legacy_transport_sha256':{name:hashlib.sha256((args.legacy_root/'agent_runtime'/name).read_bytes()).hexdigest()
                                   for name in ('tau_adapter.py','benchmark_transport.py','model_broker.py')}})
    config=TextRunConfig(domain='airline',task_split_name='base',llm_agent='deepseek-flash',llm_user='deepseek-flash',
        llm_args_agent={'temperature':0.,'max_tokens':2048},llm_args_user={'temperature':0.,'max_tokens':2048},
        num_trials=args.trials,max_concurrency=1,max_retries=0,hallucination_retries=0,auto_resume=False,
        auto_review=False,seed=args.seed,max_steps=200)
    selected=tasks[:args.tasks]
    result_sets={arm:Results(info=get_info(config),tasks=selected,simulations=[]) for arm in ('runtime','direct')}
    completed=[];failed=[];calls=[]
    atomic(args.output/'protocol.json',{'upstream':UPSTREAM,'official_files_verified':verified,'config':config.model_dump(mode='json'),
        'native_commit':commit,'native_execution':'NativeTauTransport -> AIOSKernelRuntime LLM Syscall -> shared broker',
        'simulator':'DeepSeek Flash direct broker in both arms','tools':'unchanged official coordinator',
        'pilot':args.tasks<50,'budget_before':ledger,'cost_currency':'CNY conservative peak rate, not invoice',
        'billing':'Original shared ledger retained; unknown failed calls keep full reservation',
        'budget_includes':['agent','simulator','judge'],'ranking':None})
    provider=BrokerProvider(client)
    delegated=None
    if os.environ.get('NATIVE_TAU_DELEGATED')=='1':
        relative=Path('/proc/self/cgroup').read_text().split('0::',1)[1].strip()
        group=Path('/sys/fs/cgroup')/relative.lstrip('/')
        control=group/'control'; control.mkdir(exist_ok=True)
        (control/'cgroup.procs').write_text(str(os.getpid()))
        (group/'cgroup.subtree_control').write_text('+cpu +memory +pids')
        delegated=group/'attempts'; delegated.mkdir(exist_ok=True)
        (delegated/'cgroup.subtree_control').write_text('+cpu +memory +pids')
    started=time.monotonic()
    try:
        for trial in range(args.trials):
            for index,task in enumerate(selected):
                for arm in (('runtime','direct') if (index+trial)%2==0 else ('direct','runtime')):
                    print('START',trial,index,arm,flush=True)
                    t0=time.monotonic();cpu0=time.process_time()
                    before=budget.spent
                    with tempfile.TemporaryDirectory() as folder,AIOSKernelRuntime(storage_root=folder, cgroup_root=str(delegated) if delegated else None) as kernel:
                        acb=kernel.scheduler.drf_controller.register_agent('tau-agent',max_model_calls=256,max_tokens=524288)
                        kernel.scheduler.drf_controller.global_max_model_calls=256
                        kernel.scheduler.drf_controller.global_max_tokens=524288
                        def record(value):
                            value.update(task_index=index,trial=trial)
                            calls.append(value)
                            with (args.output/'calls.jsonl').open('a') as f:
                                f.write(json.dumps(value)+'\n');f.flush();os.fsync(f.fileno())
                        bridge=NativeTauTransport(kernel,provider,arm=arm,model='deepseek-flash',
                            reserve=lambda q,a:budget.reserve(q.max_new_tokens),settle=budget.settle,record=record)
                        bridge.install()
                        try:
                            sim=run_single_task(config,task,seed=args.seed+trial,evaluation_type=EvaluationType.ALL,
                                                save_dir=args.output/'official-logs')
                            sim.trial=trial;sim.seed=args.seed+trial
                            result_sets[arm].simulations.append(sim)
                            result_sets[arm].save(args.output/(arm+'-results.json'))
                            if any(row['status']!='succeeded' for row in bridge.records):raise RuntimeError('Provider failure retained; no replay')
                            value={'trial':trial,'task_index':index,'task_id':task.id,'arm':arm,
                                'reward':sim.reward_info.reward,'termination':str(sim.termination_reason),
                                'seconds':time.monotonic()-t0,'cpu_seconds':time.process_time()-cpu0,
                                'calls':len(bridge.records),'cost_or_reserved_cny':budget.spent-before,
                                'usage':{k:sum(row.get('usage',{}).get(k,0) for row in bridge.records)
                                         for k in ('prompt_tokens','completion_tokens','prompt_cache_hit_tokens')}}
                            completed.append(value);atomic(args.output/'progress.json',{'completed':completed,'failures':failed})
                            print('DONE',trial,index,arm,sim.reward_info.reward,round(value['seconds'],3),flush=True)
                        finally:bridge.close()
    except Exception as exc:
        failed.append({'type':type(exc).__name__,'no_replay':True,'trial':trial,'task_index':index,'arm':arm,'task_id':task.id})
    finally:
        summary={'official_commit':UPSTREAM,'completed':completed,'failures':failed,'expected':args.tasks*args.trials*2,
                 'wall_seconds':time.monotonic()-started,'budget_after':json.loads(args.ledger.read_text()),
                 'maxrss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'ranking':None,
                 'full_coverage':len(completed)==100*args.trials,'official_acceptance':False}
        atomic(args.output/'summary.json',summary)
        print('SUMMARY',json.dumps({k:v for k,v in summary.items() if k!='completed'}),flush=True)
    raise SystemExit(1 if failed else 0)

if __name__=='__main__':main()
