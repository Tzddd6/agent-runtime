"""Frozen repeated Runtime vs direct tool-loop comparison, synthetic fixtures.

Same initial tasks, model, tools, context governor, opportunities and total
resource envelope. Direct calls still get actual tool access; API-only guessing
cannot be a fair browser/engineering baseline. Every paid intent is persisted.
"""
import argparse,base64,hashlib,io,json,os,statistics,subprocess,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tools'))
ARMS=('direct_loop','runtime');LIMITS={'cpu_milli':2000,'memory_bytes':1073741824,'pids':224}
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2));path.chmod(0o600)
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def prepare(folder):
    from tools.engineering_cases import config_case,logs_case
    folder=Path(folder);folder.mkdir(mode=0o700,parents=True,exist_ok=False)
    cases=[{'id':'web_bare','group':'web','prompt':'你去读下mc.tzddd.cn，只说明实际看到的页面、来源与HTTP状态。','expected':['登录','https://mc.tzddd.cn']},
           {'id':'web_python','group':'web','prompt':'读取https://www.python.org/，概括网站用途，注明实际来源和HTTP状态。','expected':['Python','https://www.python.org/']},
           {'id':'web_auth','group':'web','prompt':'读取https://mc.tzddd.cn/agentrt/，如果要求登录，直接如实报告HTTP状态，不使用任何凭据。','expected':['401']},
           {'id':'upload_docs','group':'upload','prompt':'不需要工具，结合所附PDF和Word，只输出JSON：pdf_budget为PDF预算，word_budget为Word预算。','fixture':'docs','expected':{'pdf_budget':95,'word_budget':73}},
           {'id':'upload_image','group':'upload','prompt':'不需要工具，读取上传图片，只输出JSON字段code，值为图中的验证码。','fixture':'image','expected':{'code':'QUARTZ-729'}},
           {'id':'upload_csv','group':'upload','prompt':'读取所附CSV，计算订单总金额（分），必须实际将严格JSON {"total_cents":整数}写入receipt.json并读取核对。','fixture':'csv','expected':{'total_cents':875}},
           {'id':'repair_pricing','group':'repair','prompt':'修复pricing项目','project':'pricing'},
           {'id':'browser_lab','group':'computer','prompt':'打开专用电脑实验桌面，查询PINE-442，核对预算95元和负责人Cedar。','expected':['PINE-442：预算95元，负责人Cedar。']}]
    for factory in (config_case,logs_case):
        fixture=factory(2026111619)
        cases.append({'id':'repair_'+fixture['family'],'group':'repair','prompt':'修复这个Python工程，不得修改测试。'+fixture['requirements'],'files':fixture['files'],'extra_test':fixture['extra_test'],'project':None})
    write(folder/'dataset.json',{'cases':cases})
    protocol={'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'runner_sha256':sha(__file__),'dataset_sha256':sha(folder/'dataset.json'),
        'cases':10,'repeats':2,'samples':40,'arms':list(ARMS),'resources':{**LIMITS,'pids':256},'runtime_admission_pids':224,'runtime_workers':2,'per_sample_model_calls':6,'global_model_intent_cap':120,
        'single_call_output_max':4096,'computer_call_output_max':1024,'thinking':False,'order':'case/repeat rotated paired order; fresh process/cgroup per sample',
        'quality':'actual page result or attachment witness or exact file JSON; engineering must pass protected original and external oracle, unchanged tests and delivered hashes',
        'fairness':'same auto_tasks, registered tools, ContextGovernor, protected checker and model broker; direct sequential tool loop excludes Runtime Worker orchestration',
        'limits':['small synthetic tasks, two repetitions, not significance','supplier sampling/cache/network uncontrolled','shared broker/cloud outside local sample cgroup','public sites may change; source digests recorded','browser done is not quality proof']}
    write(folder/'protocol.json',protocol)

def fixtures(case,sample,workspace):
    from agent_runtime.attachments import AttachmentStore
    from agent_runtime.database import RuntimeDatabase
    from test_document_uploads import pdf,docx
    files={}
    if case.get('fixture')=='docs':files={'facts.pdf':pdf(),'facts.docx':docx()}
    elif case.get('fixture')=='csv':files={'orders.csv':b'name,units,price_cents\nA,3,125\nB,2,250\n'}
    elif case.get('fixture')=='image':
        from PIL import Image,ImageDraw,ImageFont
        image=Image.new('RGB',(640,180),'white');draw=ImageDraw.Draw(image)
        font_path=Path('/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf')
        if not font_path.is_file():font_path=next(Path('/usr/share/fonts').rglob('DejaVuSans.ttf'))
        draw.text((28,50),'QUARTZ-729',fill='black',font=ImageFont.truetype(str(font_path),58));buffer=io.BytesIO();image.save(buffer,format='PNG');files={'witness.png':buffer.getvalue()}
    for name,content in case.get('files',{}).items():
        target=workspace/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(content)
    if files:
        store=AttachmentStore(sample/'attachments',RuntimeDatabase(sample/'metadata'));ids=[]
        for name,raw in files.items():ids.append(store.upload({'conversation_id':'a'*32,'name':name,'data':base64.b64encode(raw).decode()})['id'])
        return store.context(ids,'a'*32,workspace)
    return None

def direct(tasks,workspace,sample,client):
    from agent_runtime.runtime import validate
    from agent_runtime.context import ContextStore
    from agent_runtime.ipc import Broker
    from agent_runtime.model_gateway import ModelGateway
    from agent_runtime.worker import perform_plugin
    from agent_runtime.routing import route_plan,repair_plan
    from agent_runtime.web_flow import finish
    from agent_runtime.repair_loop import execute
    store=ContextStore(sample/'contexts');broker=Broker(store,[]);gateway=ModelGateway(client,sample/'model-calls.jsonl');pending=list(tasks);outputs={};states={};events=[]
    try:
        validate(pending)
        while any(states.get(t['id'])!='succeeded' for t in pending):
            task=next(t for t in pending if t['id'] not in states and all(states.get(k)=='succeeded' for k in t.get('depends_on',[])))
            key=task['id'];agent=task.get('agent',key);directory=sample/key;directory.mkdir();refs=[store.put(v,['$runtime',agent]) for v in task.get('contexts',[])];inputs={k:store.put(outputs[k],['$runtime',agent]) for k in task.get('inputs',[])}
            for ref in task.get('_shared_refs',[]):store.grant(ref,'$runtime',agent)
            token=broker.issue(agent);payload={**task,'_contexts':task.get('contexts',[]),'_inputs':{k:outputs[k] for k in task.get('inputs',[])},'_input_refs':inputs,'_context_refs':refs,'_workspace':str(directory),'_project_workspace':str(workspace),'_ipc_url':broker.url,'_ipc_token':token}
            children=[];action=task['action']
            if action=='llm':
                view=broker.contexts.view(agent,refs,inputs,task.get('context_query') or '\n'.join(m['content'] for m in task.get('messages',[]))[:2000]);messages=list(task['messages'])
                if view['images']:messages.append({'role':'system','content':'agentrt-internal-images-v1:'+json.dumps(view['images'])})
                if task.get('contexts') or task.get('inputs'):messages.append({'role':'user','content':json.dumps(view['body'],ensure_ascii=False)})
                value=client.complete(messages,task.get('max_tokens',256))
                if value.get('finish_reason')=='length':raise ValueError('incomplete response')
                output=value['content']
            elif action=='plugin':
                if task['plugin']=='computer.run':payload.update(_gateway_url=gateway.url,_gateway_token=gateway.issue(agent))
                output=perform_plugin(payload)
            elif action=='route_gate':
                try:output,children=route_plan(payload)
                except ValueError as error:output,children=repair_plan(payload,error)
            elif action=='web_gate':output,children=finish(payload)
            elif action in ('repair_prepare','repair_check'):output,children=execute(payload)
            else:raise ValueError('Unsupported declared baseline action')
            states[key]='succeeded';outputs[key]=output;events.append({'task_id':key,'state':'succeeded','result':{'result':output}});pending+=children
            broker.revoke(token)
            if len(pending)>64:raise ValueError('task bound')
        return states,events
    finally:gateway.close();broker.close()

def grade(case,answer,values,workspace,folder,tag):
    if case['group']=='repair':
        from tools.benchmark_engineering_loop import grade as engineering_grade
        final=next((v for v in reversed(values) if isinstance(v,dict) and v.get('phase') in ('verified_delivery','unresolved')), {})
        result={'status':'completed','accepted':final.get('accepted',False),'source':final.get('source'),'verification':final.get('verification'),
                'files':{p.relative_to(workspace).as_posix():p.read_text() for p in workspace.rglob('*.py') if '__pycache__' not in p.parts}}
        return engineering_grade(case,result,(folder/'external-oracle').resolve(),tag)
    if case['group']=='computer':
        session=next((v for v in values if isinstance(v,dict) and v.get('plugin')=='computer.run'),{})
        passed=session.get('ok') is True and all(s in session.get('observation',{}).get('text','') for s in case['expected']) and any(s.get('action')=='click' and s.get('succeeded') for s in session.get('steps',[]))
        return {'passed':passed,'actual_observation_checked':True}
    if case['group']=='web':
        reads=[v for v in values if isinstance(v,dict) and v.get('plugin')=='web.fetch'];expected=401 if case['id']=='web_auth' else 200
        return {'passed':bool(reads) and any(v.get('http_status')==expected for v in reads) and all(s in answer for s in case['expected']),
                'actual_http_checked':True,'source_sha256':[hashlib.sha256(v.get('text','').encode()).hexdigest() for v in reads]}
    if case.get('fixture')=='csv':return {'passed':json.loads((workspace/'receipt.json').read_text())==case['expected'],'actual_artifact_checked':True}
    text=answer.strip()
    if text.startswith('```'):text='\n'.join(text.splitlines()[1:-1])
    return {'passed':json.loads(text)==case['expected'],'actual_attachment_witness_checked':True}

def child(folder,index,repeat,arm,attempts):
    assert sys.stdin.readline().strip()=='READY';folder=Path(folder);case=json.loads((folder/'dataset.json').read_text())['cases'][index];tag=f'{repeat:02d}-{index:02d}-{arm}';sample=folder/tag;workspace=sample/'workspace';workspace.mkdir(parents=True)
    from agent_runtime.model_broker import BrokerClient
    from agent_runtime.model_gateway import ModelGateway,ModelError
    from agent_runtime.routing import auto_tasks
    from agent_runtime.runtime import run
    from agent_runtime.chat_store import answer_for
    from unittest.mock import patch
    calls=[];lock=threading.Lock()
    class Client(BrokerClient):
        def complete(self,messages,max_tokens=4096):
            with lock:
                if len(calls)>=6:raise ModelError('declared sample call cap')
                row={'status':'intent','max_tokens':max_tokens,'messages_sha256':hashlib.sha256(json.dumps(messages,ensure_ascii=False).encode()).hexdigest()};calls.append(row);write(sample/'calls.json',calls)
            started=time.monotonic()
            try:
                result=super().complete(messages,max_tokens);row.update(status='returned',seconds=time.monotonic()-started,usage=result.get('usage'),finish_reason=result.get('finish_reason'));return result
            except BaseException as error:row.update(status='uncertain_or_failed',error_type=type(error).__name__);raise
            finally:write(sample/'calls.json',calls)
    client=Client.from_env();status='completed';quality={'passed':False};answer='';error=None
    try:
        attachments=fixtures(case,sample,workspace);planned=auto_tasks(case['prompt'],response_tokens=4096,attachments=attachments,workspace=workspace)
        for task in planned:
            if task.get('plugin')=='computer.run':task['arguments']['max_steps']=6
        if arm=='runtime':
            with patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda path:ModelGateway(client,path)):
                states,events=run(planned,quiet=True,max_workers=2,cgroup_root=attempts,output_dir=sample/'runs',workspace=workspace,budget=LIMITS,model_budget={'calls':6,'output_tokens':24576})
        else:states,events=direct(planned,workspace,sample,client)
        status='completed' if all(s=='succeeded' for s in states.values()) else 'error';answer=answer_for({'status':'succeeded' if status=='completed' else 'failed','preset':'auto','events':events})
        values=[e.get('result',{}).get('result') for e in events if e['state']=='succeeded'];quality=grade(case,answer,values,workspace,folder,tag)
    except BaseException as exc:status='error';error=type(exc).__name__
    if any(c['status']!='returned' for c in calls):status='transport_uncertain'
    write(sample/'result.json',{'case':case['id'],'group':case['group'],'repeat':repeat,'arm':arm,'status':status,'quality':quality,'answer':answer,'calls':calls,'error_type':error})

def study(folder):
    from agent_runtime.model_broker import BrokerClient
    folder=Path(folder);protocol=json.loads((folder/'protocol.json').read_text());assert protocol['runner_sha256']==sha(__file__) and protocol['dataset_sha256']==sha(folder/'dataset.json')
    assert protocol['source_commit']==subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    assert not (folder/'results.json').exists();base=Path('/sys/fs/cgroup')/Path('/proc/self/cgroup').read_text().split('0::')[1].strip().lstrip('/')
    (base/'control').mkdir();(base/'control/cgroup.procs').write_text(str(os.getpid()));(base/'cgroup.subtree_control').write_text('+cpu +memory +pids')
    rows=[];used=0;client=BrokerClient.from_env();cases=json.loads((folder/'dataset.json').read_text())['cases']
    for repeat in range(2):
        for index,case in enumerate(cases):
            arms=ARMS if (index+repeat)%2==0 else ARMS[::-1]
            for arm in arms:
                if used+6>120:raise RuntimeError('global paid intent guard')
                until=time.monotonic()+45
                def production_busy():
                    import urllib.request
                    request=urllib.request.Request('http://127.0.0.1:18880/api/chat/status',headers={'X-AgentRT-Proxy-Token':os.environ['AGENTRT_PROXY_TOKEN']})
                    with urllib.request.urlopen(request,timeout=5) as response:value=json.load(response)
                    if value['recovery_required']:raise RuntimeError('production recovery state')
                    return value['busy']
                while client.status()['busy'] or production_busy():
                    if time.monotonic()>until:raise RuntimeError('broker busy deadline')
                    time.sleep(.3)
                tag=f'{repeat:02d}-{index:02d}-{arm}';group=base/tag;group.mkdir()
                for key,value in {'cpu.max':'200000 100000','memory.max':str(1024**3),'memory.swap.max':'0','pids.max':'256'}.items():(group/key).write_text(value)
                (group/'control').mkdir();(group/'attempts').mkdir();(group/'cgroup.subtree_control').write_text('+cpu +memory +pids');(group/'attempts/cgroup.subtree_control').write_text('+cpu +memory +pids')
                write(folder/'results.json',{'rows':rows,'paid_intents':used,'active_intent':tag})
                proc=subprocess.Popen([sys.executable,__file__,'--child',str(folder),'--index',str(index),'--repeat',str(repeat),'--arm',arm,'--attempts',str(group/'attempts')],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                (group/'control/cgroup.procs').write_text(str(proc.pid));started=time.monotonic();proc.stdin.write(b'READY\n');proc.stdin.close()
                try:proc.wait(timeout=200)
                except subprocess.TimeoutExpired:(group/'cgroup.kill').write_text('1');proc.wait();raise RuntimeError('sample deadline; no paid replay')
                row=json.loads((folder/tag/'result.json').read_text());row.update(seconds=time.monotonic()-started,cpu_seconds=int(dict(line.split() for line in (group/'cpu.stat').read_text().splitlines())['usage_usec'])/1e6,memory_peak_bytes=int((group/'memory.peak').read_text()))
                used+=len(row['calls']);assert used<=120
                assert 'populated 0' in (group/'cgroup.events').read_text()
                for path in sorted(group.rglob('*'),key=lambda p:len(p.parts),reverse=True):
                    if path.is_dir():path.rmdir()
                group.rmdir();row['remaining_sample_cgroups']=0;rows.append(row);write(folder/'results.json',{'rows':rows,'paid_intents':used,'finished':False})
                print(json.dumps({'repeat':repeat,'case':case['id'],'arm':arm,'passed':row['quality']['passed'],'seconds':round(row['seconds'],3),'calls':len(row['calls']),'status':row['status']}),flush=True)
                if row['status']=='transport_uncertain':raise RuntimeError('uncertain supplier state; retained, no replay')
    summary={}
    for arm in ARMS:
        selected=[r for r in rows if r['arm']==arm];calls=[c for r in selected for c in r['calls']]
        summary[arm]={'samples':len(selected),'passed':sum(r['quality']['passed'] for r in selected),'calls':len(calls),'median_seconds':statistics.median(r['seconds'] for r in selected),
            'input_tokens':sum((c.get('usage') or {}).get('prompt_tokens',0) for c in calls),'output_tokens':sum((c.get('usage') or {}).get('completion_tokens',0) for c in calls),
            'cached_input_tokens':sum((c.get('usage') or {}).get('prompt_cache_hit_tokens',0) for c in calls),'median_cpu_seconds':statistics.median(r['cpu_seconds'] for r in selected),'median_memory_peak_bytes':statistics.median(r['memory_peak_bytes'] for r in selected)}
    write(folder/'results.json',{'rows':rows,'paid_intents':used,'finished':True,'summary':summary});print('INTERACTIVE_SUMMARY '+json.dumps(summary),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--prepare');parser.add_argument('--run');parser.add_argument('--child');parser.add_argument('--index',type=int);parser.add_argument('--repeat',type=int);parser.add_argument('--arm',choices=ARMS);parser.add_argument('--attempts');args=parser.parse_args()
    if args.prepare:prepare(args.prepare)
    elif args.run:study(args.run)
    elif args.child:child(args.child,args.index,args.repeat,args.arm,args.attempts)
