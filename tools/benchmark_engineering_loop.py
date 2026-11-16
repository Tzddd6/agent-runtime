"""Predeclared paired benchmark: one-call+verification, lean loop, Runtime.

All requests use the existing credential-owning broker. Never replay paid POSTs.
"""
import argparse,hashlib,json,os,random,statistics,subprocess,sys,threading,time,urllib.request
from pathlib import Path
REPO=Path(__file__).resolve().parents[1];sys.path.insert(0,str(REPO));sys.path.insert(0,str(REPO/'tools'))
from tools.compare_direct_runtime import write_json,fresh_cases,object_from
ARMS=['direct_verified','lean_loop','runtime'];SEED=20261116017


def hash_file(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def fixture_files(folder,files):
    folder.mkdir(parents=True,exist_ok=True)
    for name,content in files.items():
        target=folder/name;assert target.resolve().is_relative_to(folder.resolve());target.parent.mkdir(parents=True,exist_ok=True);target.write_text(content)


def prepare(folder):
    from engineering_cases import config_case,logs_case
    from agent_runtime.engineering import manifest,run_check,BASELINE_SOURCE
    from agent_runtime.capabilities import execute_plugin
    from test_additional_projects import INTERVAL_REFERENCE,TEXT_REFERENCE
    root=Path(folder);root.mkdir(parents=True,exist_ok=True)
    assert not (root/'protocol.json').exists(),'Do not overwrite frozen protocol'
    cases=[];rng=random.Random(SEED)
    reference={'pricing':BASELINE_SOURCE.replace('unit_cents * quantity - discount_pct','unit_cents * quantity * (100 - discount_pct) // 100').replace('subtotal_cents > 5000','subtotal_cents >= 5000').replace('paid_cents // total_qty * returned_qty','paid_cents * returned_qty // total_qty'),'intervals':INTERVAL_REFERENCE,'text_normalization':TEXT_REFERENCE}
    for repeat in range(2):
        for project in reference:
            spec=manifest(project)
            if repeat==0:
                assert not run_check(spec['source'],root/('oracle-red-'+project),project)['success']
                assert run_check(reference[project],root/('oracle-green-'+project),project)['success']
            cases.append({'id':project+'_'+str(repeat),'group':'engineering','family':project,'project':project,'repeat':repeat,
                'prompt':'修复这个Python项目，真实验证，保持接口。只返回JSON {"source":"完整源码"}。遵守受控Python子集，不添加导入、额外函数、类型注解、循环或三元表达式。\n'+json.dumps(spec,ensure_ascii=False)})
    for repeat in range(3):
        for factory in (config_case,logs_case):
            fixture=factory(SEED+repeat);files=fixture['files'];family=fixture['family']
            if repeat==0:
                for mode,changes in [('red',{}),('green',fixture['golden'])]:
                    path=root/f'oracle-{family}-{mode}';fixture_files(path,{**files,**changes,'test_extra.py':fixture['extra_test']})
                    result=execute_plugin({'plugin':'workspace.test','arguments':{},'_workspace':str(path)})
                    assert result['success']==(mode=='green'),result
            cases.append({'id':family+'_'+str(repeat),'group':'engineering','family':family,'project':None,'repeat':repeat,'files':files,'extra_test':fixture['extra_test'],
                'prompt':'修复这个Python工程并真实验证，不得修改测试。'+fixture['requirements']})
    for i in range(4):
        a,b=rng.randrange(12,90),rng.randrange(2,12)
        cases.append({'id':'plain_'+str(i),'group':'plain','family':'plain','repeat':i,'prompt':f'不需要工具。只输出JSON字段value，值为({a}+3)*{b}//2。','expected':{'value':(a+3)*b//2}})
    rng.shuffle(cases)
    for i,case in enumerate(cases):
        arms=ARMS if case['group']=='engineering' else ['direct_verified','runtime']
        shift=i%len(arms);case['order']=arms[shift:]+arms[:shift]
    write_json(root/'dataset.json',{'cases':cases})
    protocol={'seed':SEED,'date':'2026-11-16 Asia/Shanghai','source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
        'dataset_sha256':hash_file(root/'dataset.json'),'runner_sha256':hash_file(__file__),'fixture_sha256':hash_file(REPO/'tools/engineering_cases.py'),
        'engineering_cases':12,'engineering_executions':36,'plain_cases':4,'plain_executions':8,
        'arms':{'direct_verified':'single model completion + identical local validation; no correction','lean_loop':'same task-specific state machine and prompts, at most one correction, no Runtime DAG','runtime':'public auto_tasks entry, actual Workers and cgroups, at most one correction'},
        'model':'same existing broker default; native thinking disabled','output_per_call':4096,'max_calls':{'direct_verified':1,'lean_loop':2,'runtime':2},
        'sample_resources':{'cpu_milli':2000,'memory_bytes':1073741824,'pids':128},'runtime_workers':2,
        'global_paid_intent_cap':96,'global_requested_output_cap':393216,
        'timing':'fresh process start to terminal verification and artifact; provider completion timings reported separately; external oracle grade excluded in every arm',
        'quality':'prevalidated red/green oracle; preserved tests, matching delivered source hashes, local verification and independent unseen tests all required',
        'fairness':'same initial task data, same system instructions and validation for engineering; lean loop uses same trusted state machine to isolate orchestration overhead',
        'failure_policy':'retain every sample; transport uncertainty halts study, no paid replay; no prompt tuning after observing results',
        'limits':['small synthetic multi-file projects and fixed controlled fixtures','model sampling and supplier cache uncontrolled','local cgroup excludes shared broker and cloud','not industrial repositories or statistical significance']}
    write_json(root/'protocol.json',protocol);print(json.dumps(protocol,ensure_ascii=False))


def messages_for(task):
    return task['messages']+[{'role':'user','content':json.dumps({'context':task['contexts'],'upstream_results':{}},ensure_ascii=False)}]


def child(folder,index,arm,attempts):
    if sys.stdin.readline().strip()!='READY':raise RuntimeError('Resource admission missing')
    from agent_runtime.model_broker import BrokerClient
    from agent_runtime.model_gateway import ModelGateway,ModelError
    from agent_runtime.routing import auto_tasks
    from agent_runtime.runtime import run
    from agent_runtime.repair_loop import execute,tasks
    from unittest.mock import patch
    root=Path(folder);case=json.loads((root/'dataset.json').read_text())['cases'][index];calls=[];lock=threading.Lock()
    tag=f'{index:02d}-{arm}';sample=root/tag;workspace=sample/'workspace';fixture_files(workspace,case.get('files',{}))
    class Client(BrokerClient):
        def complete(self,messages,max_tokens=4096):
            with lock:
                if len(calls)>=(1 if arm=='direct_verified' else 2):raise ModelError('Declared call cap before provider')
                row={'status':'intent','max_tokens':max_tokens,'message_bytes':len(json.dumps(messages,ensure_ascii=False).encode()),'messages_sha256':hashlib.sha256(json.dumps(messages,ensure_ascii=False).encode()).hexdigest()};calls.append(row);write_json(root/(tag+'-calls.json'),calls)
            start=time.monotonic()
            try:
                result=super().complete(messages,max_tokens)
                row.update(status='returned',seconds=time.monotonic()-start,usage=result.get('usage'),finish_reason=result.get('finish_reason'),model=result.get('model'))
                write_json(root/(tag+'-calls.json'),calls);return result
            except BaseException as exc:
                row.update(status='error_or_uncertain',error_type=type(exc).__name__);write_json(root/(tag+'-calls.json'),calls);raise
    client=Client.from_env();events=[];states={};answer='';final={};status='completed';source=None
    try:
        if case['group']=='plain':
            if arm=='direct_verified':answer=client.complete(([{'role':'system','content':case['system']}] if case.get('system') else [])+[{'role':'user','content':case['prompt']}],4096)['content']
            else:
                from agent_runtime.chat_store import answer_for
                with patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda p:ModelGateway(client,p)):
                    states,events=run(auto_tasks(case['prompt']),quiet=True,max_workers=2,output_dir=sample/'runs',cgroup_root=attempts,workspace=workspace,budget={'cpu_milli':2000,'memory_bytes':1073741824,'pids':128})
                answer=answer_for({'preset':'auto','status':'succeeded' if all(v=='succeeded' for v in states.values()) else 'failed','events':events})
        elif arm=='runtime':
            with patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda p:ModelGateway(client,p)):
                states,events=run(auto_tasks(case['prompt'],workspace=workspace),quiet=True,max_workers=2,output_dir=sample/'runs',cgroup_root=attempts,workspace=workspace,budget={'cpu_milli':2000,'memory_bytes':1073741824,'pids':128})
            candidates=[e['result']['result'] for e in events if e['state']=='succeeded' and e['task_id'].startswith('repair_check_')]
            final=candidates[-1] if candidates else {};source=final.get('source')
        else:
            initial=tasks(case['prompt'],case['project'])[0];initial.update(_contexts=initial['contexts'],_workspace=str(sample/'prepare'),_project_workspace=str(workspace));Path(initial['_workspace']).mkdir()
            _,children=execute(initial)
            for round_number in range(1,(1 if arm=='direct_verified' else 2)+1):
                model,gate=children;response=client.complete(messages_for(model),4096)['content']
                gate.update(_contexts=gate['contexts'],_inputs={model['id']:response},_workspace=str(sample/f'check-{round_number}'),_project_workspace=str(workspace));Path(gate['_workspace']).mkdir()
                final,children=execute(gate)
                if case['project']:
                    try:source=json.loads(response).get('source')
                    except ValueError:source=None
                if final.get('accepted') or not children:break
    except BaseException as exc:status='error';final={'error_type':type(exc).__name__}
    if any(c['status']!='returned' for c in calls):status='transport_uncertain'
    files={p.relative_to(workspace).as_posix():p.read_text() for p in workspace.rglob('*.py') if '__pycache__' not in p.parts}
    receipt={'status':status,'calls':calls,'states':states,'answer':answer,'accepted':final.get('accepted',False),'source':source,'files':files,
             'verification':final.get('verification'),'workspace':str(workspace)}
    write_json(root/(tag+'-result.json'),receipt)


def grade(case,result,root,tag):
    from agent_runtime.engineering import validate_patch,PROJECTS,RUNNER,_execute
    from agent_runtime.capabilities import execute_plugin
    if case['group']=='plain':
        value,strict=object_from(result['answer']);return {'passed':result['status']=='completed' and value==case['expected'],'json_format':strict}
    if case['project']:
        try:
            source=validate_patch({'source':result['source']},case['project']);folder=root/('grade-'+tag);folder.mkdir();runner=folder/'trusted_runner.py';runner.write_text(RUNNER);testfile=folder/'cases.json'
            testfile.write_text(json.dumps({'public':PROJECTS[case['project']]['public'],'heldout':fresh_cases(case['project'])}))
            checked=_execute(source,folder/'candidate',runner,testfile,PROJECTS[case['project']]['file'])
            same=bool(result.get('verification')) and result['verification'].get('source_sha256')==hashlib.sha256(source.encode()).hexdigest()
            return {'passed':result['status']=='completed' and result['accepted'] and checked['success'] and same,'external_passed':checked['success'],'same_verified_source':same}
        except (ValueError,TypeError):return {'passed':False,'patch_rejected':True}
    original=case['files'];files=result['files'];tests_unchanged=all(files.get(k)==v for k,v in original.items() if k.startswith('test'))
    folder=root/('grade-'+tag);fixture_files(folder,{**files,'test_extra.py':case['extra_test']})
    checked=execute_plugin({'plugin':'workspace.test','arguments':{},'_workspace':str(folder)})
    v=result.get('verification') or {};same=all(hashlib.sha256(files[k].encode()).hexdigest()==sha for k,sha in v.get('source_hashes',{}).items()) and bool(v.get('source_hashes'))
    return {'passed':result['status']=='completed' and result['accepted'] and checked['success'] and tests_unchanged and same,'external_passed':checked['success'],'tests_unchanged':tests_unchanged,'same_verified_source':same,'oracle_tests':checked['test_cases'],'oracle_output':checked['output']}


def run_study(folder):
    from agent_runtime.web import delegated_attempts
    from agent_runtime.model_broker import BrokerClient
    root=Path(folder);protocol=json.loads((root/'protocol.json').read_text());cases=json.loads((root/'dataset.json').read_text())['cases']
    assert protocol['dataset_sha256']==hash_file(root/'dataset.json') and protocol['runner_sha256']==hash_file(__file__)
    assert protocol['source_commit']==subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip()
    assert not (root/'results.json').exists(),'Existing intent/results must not be replayed'
    base=Path(delegated_attempts());client=BrokerClient.from_env();report={'protocol':protocol,'rows':[],'finished':False};write_json(root/'results.json',report);used=0
    for index,case in enumerate(cases):
        for arm in case['order']:
            assert used+2<=protocol['global_paid_intent_cap']
            end=time.monotonic()+60
            while True:
                req=urllib.request.Request('http://127.0.0.1:18880/api/chat/status',headers={'X-AgentRT-Proxy-Token':os.environ['AGENTRT_PROXY_TOKEN']})
                with urllib.request.urlopen(req,timeout=5) as f:s=json.load(f)
                if s['recovery_required']:raise RuntimeError('Production recovery flag; no model request')
                if not s['busy'] and not client.status()['busy']:break
                if time.monotonic()>end:raise RuntimeError('Busy deadline, no request started')
                time.sleep(.5)
            tag=f'{index:02d}-{arm}';group=base/tag;group.mkdir()
            for name,value in {'cpu.max':'200000 100000','memory.max':str(1024**3),'memory.swap.max':'0','pids.max':'128','cgroup.subtree_control':'+cpu +memory +pids'}.items():(group/name).write_text(value)
            control=group/'control';control.mkdir();attempts=group/'attempts';attempts.mkdir();(attempts/'cgroup.subtree_control').write_text('+cpu +memory +pids')
            report['active_intent']={'index':index,'case':case['id'],'arm':arm};write_json(root/'results.json',report)
            start=time.monotonic();proc=subprocess.Popen([sys.executable,__file__,'--child',str(root),'--index',str(index),'--arm',arm,'--attempts',str(attempts)],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            (control/'cgroup.procs').write_text(str(proc.pid));proc.stdin.write(b'READY\n');proc.stdin.close()
            try:proc.wait(timeout=160)
            except subprocess.TimeoutExpired:(group/'cgroup.kill').write_text('1');proc.wait(timeout=5);raise RuntimeError('Deadline, remote billing unknown. Do not replay')
            result=json.loads((root/(tag+'-result.json')).read_text());wall=time.monotonic()-start
            row={'case':case['id'],'family':case['family'],'group':case['group'],'repeat':case['repeat'],'arm':arm,'wall_seconds':wall,
                 'cpu_seconds':int(dict(line.split() for line in (group/'cpu.stat').read_text().splitlines())['usage_usec'])/1e6,'memory_peak_bytes':int((group/'memory.peak').read_text()),
                 'status':result['status'],'calls':result['calls'],'accepted':result['accepted'],'quality':grade(case,result,root,tag)}
            used+=len(result['calls']);report['rows'].append(row);report.pop('active_intent',None);report['paid_intents']=used;write_json(root/'results.json',report)
            print(json.dumps({k:row[k] for k in ('case','arm','wall_seconds','status')}|{'quality':row['quality']['passed'],'calls':len(row['calls'])}),flush=True)
            if 'populated 0' not in (group/'cgroup.events').read_text():raise RuntimeError('Cleanup unconfirmed; stop')
            for p in sorted(group.rglob('*'),key=lambda p:len(p.parts),reverse=True):
                if p.is_dir():p.rmdir()
            group.rmdir()
            if result['status']=='transport_uncertain':raise RuntimeError('Transport uncertainty; no paid replay')
    report.update(finished=True,remaining_sample_cgroups=sum(p.is_dir() for p in base.iterdir()));write_json(root/'results.json',report)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--prepare');p.add_argument('--run');p.add_argument('--child');p.add_argument('--index',type=int);p.add_argument('--arm',choices=ARMS);p.add_argument('--attempts');a=p.parse_args()
    if a.prepare:prepare(a.prepare)
    elif a.run:run_study(a.run)
    elif a.child:child(a.child,a.index,a.arm,a.attempts)
    else:p.error('Select prepare, run or child')
