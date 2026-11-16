"""Public execution metadata, never model reasoning, prompts, credentials or output."""
import time

ACTIVE={'running','cancelling'}
LABELS={'route':'判断需求与规划路由','route_gate':'校验协作计划','auto_answer':'汇总最终回答',
        'assistant':'生成回答','plan':'制定计划','analyze':'分析内容','index':'索引文本',
        'test_plan':'设计测试','review':'评审与汇总','baseline':'复现原始测试',
        'repair':'生成补丁','validate':'执行真实测试','gate':'核对最终结论'}

def execution_view(job,now=None):
    now=time.time() if now is None else now
    end=job.get('finished') or now
    records={}
    routing=None
    artifacts=[]
    def model_step(key):
        if key.startswith('repair_'):return key.startswith('repair_candidate_')
        mode=job.get('preset')
        return (mode=='auto' and key!='route_gate' and not key.startswith('auto_cycle_') or mode in ('llm','team') or mode=='review' and key.startswith(('writer-','critic-'))
                or mode=='engineering' and key in ('analyze','test_plan','repair','review'))
    def label(key):
        if key=='repair_prepare':return '观察工程并复现原始测试'
        if key.startswith('repair_candidate_'):return '生成源码补丁 · 第 '+key.rsplit('_',1)[-1]+' 次'
        if key.startswith('repair_check_'):return '真实验证与交付 · 第 '+key.rsplit('_',1)[-1]+' 次'
        if key.startswith('auto_decide_'):return '检查工具结果 · 第 '+key.rsplit('_',1)[-1]+' 轮'
        if key.startswith('auto_cycle_'):return '校验后续计划 · 第 '+key.rsplit('_',1)[-1]+' 轮'
        for prefix,title in (('writer-','起草'),('critic-','评审'),('gate-','核对')):
            if key.startswith(prefix):return title+'第 '+key.rsplit('-',1)[-1]+' 轮'
        return LABELS.get(key,key)
    for event in job.get('events',[]):
        key=event.get('task_id');attempt=event.get('attempt',0)
        if not isinstance(key,str):continue
        if key=='route_gate' and event.get('state')=='succeeded':
            result=event.get('result',{}).get('result',{})
            if isinstance(result,dict):routing={k:result[k] for k in ('route','reason','tasks','roles','plugins','skills') if k in result}
        pair=(key,attempt)
        step=records.setdefault(pair,{'id':key,'attempt':attempt,'agent':event.get('agent',key),
                                     'label':label(key),'kind':'model' if model_step(key) else 'local'})
        if event.get('agent')=='clock':step.update(kind='local',label='读取服务器时间')
        if key.startswith('auto_') and key!='auto_answer' and not key.startswith(('auto_decide_','auto_cycle_')):
            role=step['agent'].split('_',1)[0]
            title={'analyst':'分析','coder':'代码方案','tester':'测试设计','reviewer':'评审','researcher':'资料分析','tool':'调用工具'}.get(role,'协作任务')
            step['label']=title+' · '+key[5:]
            if role=='tool':step['kind']='local'
        call=event.get('result',{}).get('model_call',{})
        value=event.get('result',{}).get('result',{})
        if isinstance(value,dict) and key.startswith('repair_check_') and value.get('accepted'):
            verified=value.get('verification',{})
            if verified.get('written'):artifacts.append({k:verified[k] for k in ('path','bytes','sha256') if k in verified})
            for item in verified.get('files',[]):
                if item.get('written'):artifacts.append({k:item[k] for k in ('path','bytes','sha256') if k in item})
        if isinstance(value,dict) and value.get('plugin') in ('file.write','file.copy','file.replace','artifact.write','project.patch_check') and value.get('written'):
            artifacts.append({k:value[k] for k in ('path','bytes','sha256') if k in value})
        if call:
            step['thinking_requested']=bool(call.get('thinking_requested'))
            usage=call.get('usage',{})
            step['reasoning_tokens']=usage.get('completion_tokens_details',{}).get('reasoning_tokens',0)
        if event.get('state')=='running':step['started']=event['time']
        elif event.get('state')!='waiting' or attempt:
            step['finished']=event.get('time',end)
        step['status']='retrying' if event.get('state')=='waiting' and attempt else event.get('state','waiting')
        from .failures import event_failure
        problem=event_failure(event)
        if problem:step['failure']=problem
    for row in (job.get('monitor') or {}).get('tasks',[]):
        key=row['id'];pair=(key,row.get('attempt',0))
        if pair not in records and not any(s['id']==key for s in records.values()):
            records[pair]={'id':key,'attempt':row.get('attempt',0),'agent':row.get('agent',key),
                           'label':label(key),'kind':'model' if model_step(key) else 'local','status':row['state']}
    steps=[]
    for step in records.values():
        started=step.pop('started',None);finished=step.pop('finished',None)
        step['elapsed_seconds']=max(0,(finished or end)-started) if started is not None else None
        # A lost process cannot still be described as currently running.
        if job['status'] not in ACTIVE and step['status']=='running':step['status']='interrupted'
        steps.append(step)
    resources=job.get('model_resources') or (job.get('monitor') or {}).get('model_resources') or {}
    return {'status':job['status'],'elapsed_seconds':max(0,end-job['created']),
            'context_control':job.get('context_control_summary') or (job.get('monitor') or {}).get('context_control'),
            'steps':steps,'model_admitted':resources.get('used',{}).get('calls'),
            'source':'runtime_events','thinking_content':False,'routing':routing,'artifacts':artifacts}
