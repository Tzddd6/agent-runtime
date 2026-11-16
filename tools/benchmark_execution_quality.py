"""Frozen four-arm real engineering comparison, equal two-candidate opportunities.

Full-source direct, compact direct, compact Runtime spawn, compact Runtime fork.
Uses existing protected broker, never reads a supplier key or replays paid POSTs.
"""
import argparse,hashlib,json,os,random,subprocess,sys,threading,time,urllib.request
from pathlib import Path
REPO=Path(__file__).resolve().parents[1];sys.path.insert(0,str(REPO));sys.path.insert(0,str(REPO/'tools'))
from tools.benchmark_engineering_loop import fixture_files,grade,prepare as prepare_fixtures
from tools.compare_direct_runtime import write_json
ARMS=['direct_full','direct_compact','runtime_spawn','runtime_fork']


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(folder):
    folder=Path(folder);folder.mkdir(mode=0o700,exist_ok=False)
    prepare_fixtures(folder/'fixture-preparation') # Red/green oracles, zero supplier requests.
    cases=[case for case in json.loads((folder/'fixture-preparation/dataset.json').read_text())['cases']
           if case['group']=='engineering' and (case['project'] or case['repeat']==0)]
    assert len(cases)==8
    from agent_runtime.engineering import manifest
    for index,case in enumerate(cases):
        if case['project']:
            case['prompt']='修复这个Python项目并真实验证，保持全部接口及受控AST子集，交付完整已测源码。\n'+json.dumps(manifest(case['project']),ensure_ascii=False)
        shift=index%4;case['order']=ARMS[shift:]+ARMS[:shift]
    write_json(folder/'dataset.json',{'cases':cases})
    write_json(folder/'protocol.json',{'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
        'runner_sha256':digest(__file__),'dataset_sha256':digest(folder/'dataset.json'),'cases':8,'executions':32,
        'arms':ARMS,'max_calls_each_sample':2,'global_max_paid_intents':64,'max_output_each_call':4096,
        'resources':{'cpu_milli':2000,'memory_bytes':1073741824,'pids':128},'runtime_workers':2,
        'quality':'same immutable public tests + independent outside-workspace oracle, preserve every failed case',
        'method':'rotated arm order, all end-to-end times include the same local verification; external grading excluded',
        'limits':['two repeats for controlled families, one per multi-file family; no significance claim','provider sampling/cache/network not controlled; cache usage reported','prompt protocols differ by design; compact direct separates protocol benefit from Runtime overhead']})


def messages_for(task):
    contexts=json.loads(json.dumps(task.get('contexts',[]),ensure_ascii=False,sort_keys=True))
    return list(task['messages'])+[{'role':'user','content':json.dumps({'context':contexts,'upstream_results':{}},ensure_ascii=False)}]


def child(folder,index,arm,attempts):
    sys.stdin.readline();folder=Path(folder);case=json.loads((folder/'dataset.json').read_text())['cases'][index]
    from agent_runtime.model_broker import BrokerClient
    from agent_runtime.model_gateway import ModelGateway,ModelError
    from agent_runtime.runtime import run
    from agent_runtime.routing import auto_tasks
    from agent_runtime.repair_loop import execute,tasks
    from unittest.mock import patch
    tag=f'{index:02d}-{arm}';sample=folder/tag;workspace=sample/'workspace';fixture_files(workspace,case.get('files',{}))
    calls=[];lock=threading.Lock()
    class Client(BrokerClient):
        def complete(self,messages,max_tokens=4096):
            with lock:
                if len(calls)>=2:raise ModelError('sample call budget exhausted')
                row={'status':'intent','message_sha256':hashlib.sha256(json.dumps(messages,ensure_ascii=False).encode()).hexdigest(),
                     'message_bytes':len(json.dumps(messages,ensure_ascii=False).encode()),'max_tokens':max_tokens}
                calls.append(row);write_json(folder/(tag+'-calls.json'),calls)
            started=time.monotonic()
            try:
                result=super().complete(messages,max_tokens)
                row.update(status='returned',seconds=time.monotonic()-started,usage=result.get('usage'),finish_reason=result.get('finish_reason'))
                write_json(folder/(tag+'-calls.json'),calls);return result
            except BaseException as error:
                row.update(status='uncertain_or_failed',error_type=type(error).__name__);write_json(folder/(tag+'-calls.json'),calls);raise
    client=Client.from_env();states={};final={};status='completed';execution={}
    try:
        if arm.startswith('runtime_'):
            with patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda path:ModelGateway(client,path)):
                states,events=run(auto_tasks(case['prompt'],workspace=workspace),quiet=True,max_workers=2,
                    output_dir=sample/'runs',cgroup_root=attempts,workspace=workspace,
                    budget={'cpu_milli':2000,'memory_bytes':1073741824,'pids':128},
                    model_budget={'calls':2,'output_tokens':8192},worker_backend='spawn' if arm=='runtime_spawn' else 'forkserver')
            values=[e['result']['result'] for e in events if e['state']=='succeeded' and e['task_id'].startswith('repair_check_')]
            final=values[-1] if values else {};execution=json.loads(next((sample/'runs').glob('*/summary.json')).read_text()).get('execution_stats',{})
            if not all(value=='succeeded' for value in states.values()):status='error'
        else:
            initial=tasks(case['prompt'],case['project'],protocol='full' if arm=='direct_full' else 'compact')[0]
            initial.update(_contexts=initial['contexts'],_workspace=str(sample/'prepare'),_project_workspace=str(workspace));Path(initial['_workspace']).mkdir()
            _,children=execute(initial)
            for round_number in (1,2):
                model,gate=children;response=client.complete(messages_for(model),4096)
                if response.get('finish_reason')=='length':raise ValueError('incomplete model output')
                gate.update(_contexts=gate['contexts'],_inputs={model['id']:response['content']},_workspace=str(sample/f'check-{round_number}'),_project_workspace=str(workspace));Path(gate['_workspace']).mkdir()
                final,children=execute(gate)
                if final.get('accepted') or not children:break
    except BaseException as error:status='error';final={'error_type':type(error).__name__}
    if any(row['status']!='returned' for row in calls):status='transport_uncertain'
    result={'status':status,'states':states,'accepted':final.get('accepted',False),'source':final.get('source'),
            'files':{p.relative_to(workspace).as_posix():p.read_text() for p in workspace.rglob('*.py') if '__pycache__' not in p.parts},
            'verification':final.get('verification'),'calls':calls,'execution':execution,'workspace':str(workspace)}
    write_json(folder/(tag+'-result.json'),result)


def run_study(folder):
    folder=Path(folder);protocol=json.loads((folder/'protocol.json').read_text())
    assert digest(__file__)==protocol['runner_sha256'] and digest(folder/'dataset.json')==protocol['dataset_sha256']
    assert not (folder/'results.json').exists(),'Never replay a previous study'
    from agent_runtime.model_broker import BrokerClient
    client=BrokerClient.from_env();rows=[];used=0
    relative=Path('/proc/self/cgroup').read_text().split('0::',1)[1].strip();base=Path('/sys/fs/cgroup')/relative.lstrip('/')
    (base/'controller').mkdir();(base/'controller/cgroup.procs').write_text(str(os.getpid()));(base/'cgroup.subtree_control').write_text('+cpu +memory +pids')
    for index,case in enumerate(json.loads((folder/'dataset.json').read_text())['cases']):
        for arm in case['order']:
            assert used+2<=64
            deadline=time.monotonic()+60
            while client.status()['busy']:
                if time.monotonic()>deadline:raise RuntimeError('broker busy; no new request started')
                time.sleep(.5)
            tag=f'{index:02d}-{arm}';group=base/tag;group.mkdir()
            for key,value in {'cpu.max':'200000 100000','memory.max':str(1024**3),'memory.swap.max':'0','pids.max':'128','cgroup.subtree_control':'+cpu +memory +pids'}.items():(group/key).write_text(value)
            (group/'control').mkdir();attempts=group/'attempts';attempts.mkdir();(attempts/'cgroup.subtree_control').write_text('+cpu +memory +pids')
            write_json(folder/'results.json',{'rows':rows,'active_intent':tag,'paid_intents':used})
            started=time.monotonic();process=subprocess.Popen([sys.executable,__file__,'--child',str(folder),'--index',str(index),'--arm',arm,'--attempts',str(attempts)],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            (group/'control/cgroup.procs').write_text(str(process.pid));process.stdin.write(b'G\n');process.stdin.close()
            try:process.wait(timeout=160)
            except subprocess.TimeoutExpired:(group/'cgroup.kill').write_text('1');process.wait();raise RuntimeError('deadline; no paid replay')
            measured=time.monotonic()-started;result=json.loads((folder/(tag+'-result.json')).read_text())
            row={'case':case['id'],'family':case['family'],'repeat':case['repeat'],'arm':arm,'seconds':measured,
                 'cpu_seconds':int(dict(line.split() for line in (group/'cpu.stat').read_text().splitlines())['usage_usec'])/1e6,
                 'memory_peak_bytes':int((group/'memory.peak').read_text()),'status':result['status'],'calls':result['calls'],
                 'quality':grade(case,result,folder,tag),'execution':result['execution']}
            used+=len(result['calls']);rows.append(row);write_json(folder/'results.json',{'rows':rows,'paid_intents':used})
            print(json.dumps({'case':case['id'],'arm':arm,'passed':row['quality']['passed'],'seconds':round(measured,3),'calls':len(result['calls'])}),flush=True)
            assert 'populated 0' in (group/'cgroup.events').read_text()
            for path in sorted(group.rglob('*'),key=lambda path:len(path.parts),reverse=True):
                if path.is_dir():path.rmdir()
            group.rmdir()
            if result['status']=='transport_uncertain':raise RuntimeError('Transport uncertain; keep evidence and stop, no retry')
    write_json(folder/'results.json',{'rows':rows,'paid_intents':used,'finished':True,'remaining_sample_cgroups':[p.name for p in base.iterdir() if p.is_dir() and p.name!='controller']})


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--prepare');parser.add_argument('--run');parser.add_argument('--child');parser.add_argument('--index',type=int);parser.add_argument('--arm',choices=ARMS);parser.add_argument('--attempts');args=parser.parse_args()
    if args.prepare:prepare(args.prepare)
    elif args.run:run_study(args.run)
    elif args.child:child(args.child,args.index,args.arm,args.attempts)
