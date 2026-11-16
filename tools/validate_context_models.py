"""Nine bounded real calls: six context A/B runs and one semantic collaboration.

Run in a delegated service with the existing proxy EnvironmentFile. No supplier
key is read, persisted or returned; accepted model calls are never replayed.
"""
import json
import os
from pathlib import Path
import statistics
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_runtime.runtime import run
from agent_runtime.web import delegated_attempts
from agent_runtime.model_broker import BrokerClient


def execute(tasks,mode,root,attempts):
    start=time.monotonic();cpu=os.times()
    states,events=run(tasks,max_workers=2,quiet=True,output_dir=root,cgroup_root=attempts,context_mode=mode,
        budget={'cpu_milli':2000,'memory_bytes':1024**3,'pids':128},model_budget={'calls':3,'output_tokens':1536})
    calls=[e['result']['model_call'] for e in events if e['state']=='succeeded' and e.get('result',{}).get('model_call')]
    summary=json.loads(next(Path(root).glob('*/summary.json')).read_text())
    worker_cpu=sum(int(dict(line.split() for line in e.get('usage',{}).get('cpu.stat','').splitlines()).get('usage_usec',0))/1e6 for e in events if e['state']=='succeeded')
    return {'seconds':time.monotonic()-start,'states':states,'calls':len(calls),'usage':{
        key:sum(c.get('usage',{}).get(key,0) for c in calls) for key in ('prompt_tokens','completion_tokens')},
        'finish_reasons':[c.get('finish_reason') for c in calls],
        'context':summary['context_control'],'cpu_seconds':os.times().user+os.times().system-cpu.user-cpu.system+worker_cpu,
        'results':{e['task_id']:e['result']['result'] for e in events if e['state']=='succeeded'}}


def main():
    destination=Path(sys.argv[1]);destination.mkdir(parents=True,exist_ok=True)
    target=destination/'report.json'
    if target.exists():raise ValueError('Do not replay real calls against an existing report')
    assert BrokerClient.from_env().status()['configured'] and not BrokerClient.from_env().status()['busy']
    attempts=delegated_attempts();report={'samples':[],'paid_post_replay':False,'max_planned_model_calls':9}
    def save():target.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    save()
    for repeat in range(3):
        a='ANALYST_'+uuid.uuid4().hex[:10];b='TEST_'+uuid.uuid4().hex[:10]
        tasks=[{'id':'analysis','agent':'analyst','action':'echo','message':{'semantic_facts':[{'subject':'plan','predicate':'analyst_witness','value':a}],
                'background':'This background paragraph describes routine record keeping and unrelated terminology. '*950}},
               {'id':'tests','agent':'tester','action':'echo','message':{'semantic_facts':[{'subject':'plan','predicate':'test_witness','value':b}],
                'background':'This background paragraph contains unrelated examples about routine documentation and maintenance. '*850}},
               {'id':'answer','agent':'reviewer','action':'llm','depends_on':['analysis','tests'],'inputs':['analysis','tests'],
                'contexts':[{'user_request':'读取 analyst_witness 和 test_witness 原始编号。必须保留测试文件，不得修改。','constraints':['不得修改测试文件']}],
                'messages':[{'role':'user','content':'读取上游semantic_facts中的analyst_witness和test_witness，原样保留编号。只输出JSON字段analyst_witness、test_witness、must_preserve_tests:true。'}],
                'context_query':'analyst_witness test_witness 必须保留测试','max_tokens':512,
                'resources':{'cpu_milli':250,'memory_bytes':96*1024**2,'pids':16}}]
        for mode in (['full','balanced'] if repeat%2==0 else ['balanced','full']):
            report['active_intent']={'repeat':repeat,'mode':mode};save()
            sample=execute(tasks,mode,destination/f'{repeat}-{mode}',attempts)
            answer=sample['results'].get('answer','')
            try:value=json.loads(answer)
            except ValueError:value={}
            sample.update(repeat=repeat,mode=mode,quality_passed=value.get('analyst_witness')==a and value.get('test_witness')==b and value.get('must_preserve_tests') is True)
            report['samples'].append(sample);report.pop('active_intent',None);save()
            print(json.dumps({'repeat':repeat,'mode':mode,'seconds':sample['seconds'],'usage':sample['usage'],'passed':sample['quality_passed']}),flush=True)
    tasks=[]
    for name,value in [('analyst',2),('reviewer',3)]:
        tasks.append({'id':name,'agent':name,'action':'llm','max_tokens':256,
            'messages':[{'role':'user','content':'你提供待核验的主张，禁止配置系统。只返回JSON，不要围栏：'+json.dumps({'semantic_facts':[{'subject':'run_config','predicate':'workers','value':value}]})}],
            'resources':{'cpu_milli':250,'memory_bytes':96*1024**2,'pids':16}})
    tasks.extend([{'id':'shared_scan','agent':'checker','action':'plugin','plugin':'context.search','arguments':{'query':'run_config workers','limit':6},'depends_on':['analyst','reviewer'],'inputs':['analyst','reviewer']},
                  {'id':'verdict','agent':'verdict','action':'llm','depends_on':['shared_scan'],'inputs':['shared_scan'],'max_tokens':256,
                   'messages':[{'role':'user','content':'只依据上游检索结果判断冲突，不配置系统。仅输出JSON needs_verification:true/false，alternatives为冲突workers值的升序数组。'}],
                   'resources':{'cpu_milli':250,'memory_bytes':96*1024**2,'pids':16}}])
    report['active_intent']={'scenario':'semantic_conflict'};save()
    shared=execute(tasks,'balanced',destination/'semantic-conflict',attempts)
    try:answer=json.loads(shared['results'].get('verdict',''))
    except ValueError:answer={}
    conflicts=shared['results'].get('shared_scan',{}).get('conflicts',[])
    shared['quality_passed']=len(conflicts)==1 and answer.get('needs_verification') is True and answer.get('alternatives')==[2,3]
    shared['provenance_is_model_claim']=all(p['kind']=='model_claim' for m in shared['results'].get('shared_scan',{}).get('matches',[]) for p in m['provenance'])
    report.update(semantic_collaboration=shared,finished=True,actual_model_calls=sum(s['calls'] for s in report['samples'])+shared['calls'])
    report.pop('active_intent',None)
    report['summary']={mode:{'samples':3,'passed':sum(s['quality_passed'] for s in report['samples'] if s['mode']==mode),
        'median_seconds':statistics.median(s['seconds'] for s in report['samples'] if s['mode']==mode),
        'actual_input_tokens':sum(s['usage']['prompt_tokens'] for s in report['samples'] if s['mode']==mode),
        'actual_output_tokens':sum(s['usage']['completion_tokens'] for s in report['samples'] if s['mode']==mode),
        'median_cpu_seconds':statistics.median(s['cpu_seconds'] for s in report['samples'] if s['mode']==mode)} for mode in ('full','balanced')}
    report['remaining_attempt_directories']=sum(p.is_dir() for p in Path(attempts).iterdir());save()
    assert report['actual_model_calls']<=9 and all(s['quality_passed'] for s in report['samples']) and shared['quality_passed'] and shared['provenance_is_model_claim']
    assert report['remaining_attempt_directories']==0
    print(json.dumps({'summary':report['summary'],'semantic_passed':shared['quality_passed'],'actual_model_calls':report['actual_model_calls']},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
