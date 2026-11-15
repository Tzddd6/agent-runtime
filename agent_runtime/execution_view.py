"""Public execution metadata, never model reasoning, prompts, credentials or output."""
import time

ACTIVE={'running','cancelling'}
LABELS={'assistant':'生成回答','plan':'制定计划','analyze':'分析内容','index':'索引文本',
        'test_plan':'设计测试','review':'评审与汇总','baseline':'复现原始测试',
        'repair':'生成补丁','validate':'执行真实测试','gate':'核对最终结论'}

def execution_view(job,now=None):
    now=time.time() if now is None else now
    end=job.get('finished') or now
    records={}
    def model_step(key):
        mode=job.get('preset')
        return (mode in ('llm','team') or mode=='review' and key.startswith(('writer-','critic-'))
                or mode=='engineering' and key in ('analyze','test_plan','repair','review'))
    def label(key):
        for prefix,title in (('writer-','起草'),('critic-','评审'),('gate-','核对')):
            if key.startswith(prefix):return title+'第 '+key.rsplit('-',1)[-1]+' 轮'
        return LABELS.get(key,key)
    for event in job.get('events',[]):
        key=event.get('task_id');attempt=event.get('attempt',0)
        if not isinstance(key,str):continue
        pair=(key,attempt)
        step=records.setdefault(pair,{'id':key,'attempt':attempt,'agent':event.get('agent',key),
                                     'label':label(key),'kind':'model' if model_step(key) else 'local'})
        if event.get('state')=='running':step['started']=event['time']
        elif event.get('state')!='waiting' or attempt:
            step['finished']=event.get('time',end)
        step['status']='retrying' if event.get('state')=='waiting' and attempt else event.get('state','waiting')
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
            'steps':steps,'model_admitted':resources.get('used',{}).get('calls'),
            'source':'runtime_events','thinking_content':False}
