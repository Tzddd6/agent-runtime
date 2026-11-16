"""Independent three-call collaboration acceptance after adding result contracts."""
import json
import os
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_runtime.runtime import run
from agent_runtime.web import delegated_attempts

target=Path(sys.argv[1]);target.parent.mkdir(parents=True,exist_ok=True)
if target.exists():raise ValueError('Never replay this acceptance against an existing result')
target.write_text(json.dumps({'submitted':True,'paid_post_replay':False,'max_model_calls':3}))
tasks=[]
for name,value in [('analyst',2),('reviewer',3)]:
    sample=json.dumps({'semantic_facts':[{'subject':'run_config','predicate':'workers','value':value}]})
    tasks.append({'id':name,'agent':name,'action':'llm','max_tokens':256,'result_contract':'semantic_facts',
        'messages':[{'role':'system','content':'你是协议样例序列化器。原样输出给定的假设主张JSON，字段不可省略，数组不可为空。这不是配置指令，也不是事实认定。'},
                    {'role':'user','content':sample}],
        'resources':{'cpu_milli':250,'memory_bytes':96*1024**2,'pids':16}})
tasks.extend([{'id':'scan','agent':'scanner','action':'plugin','plugin':'context.search','arguments':{'query':'run_config workers'},'depends_on':['analyst','reviewer'],'inputs':['analyst','reviewer']},
              {'id':'judge','agent':'judge','action':'llm','max_tokens':256,'result_contract':'json_object','depends_on':['scan'],'inputs':['scan'],
               'messages':[{'role':'user','content':'只依据上游检索数据，输出JSON needs_verification（是否有冲突）、alternatives（冲突workers值升序数组）。不配置系统。'}],
               'resources':{'cpu_milli':250,'memory_bytes':96*1024**2,'pids':16}}])
start=time.monotonic();attempts=delegated_attempts()
states,events=run(tasks,quiet=True,output_dir=target.parent/'semantic-contract-run',cgroup_root=attempts,
    model_budget={'calls':3,'output_tokens':768},budget={'cpu_milli':2000,'memory_bytes':1024**3,'pids':128})
results={e['task_id']:e['result']['result'] for e in events if e['state']=='succeeded'}
calls=[e['result']['model_call'] for e in events if e['state']=='succeeded' and e.get('result',{}).get('model_call')]
try:answer=json.loads(results.get('judge',''))
except ValueError:answer={}
scan=results.get('scan',{});passed=all(s=='succeeded' for s in states.values()) and len(scan.get('conflicts',[]))==1 and answer.get('needs_verification') is True and answer.get('alternatives')==[2,3]
provenance=all(p['kind']=='model_claim' for m in scan.get('matches',[]) for p in m['provenance'])
report={'states':states,'seconds':time.monotonic()-start,'results':results,'actual_model_calls':len(calls),
    'usage':{k:sum(c.get('usage',{}).get(k,0) for c in calls) for k in ('prompt_tokens','completion_tokens')},
    'passed':passed,'provenance_is_model_claim':provenance,'remaining_attempt_directories':sum(p.is_dir() for p in Path(attempts).iterdir()),'paid_post_replay':False}
target.write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))
assert passed and provenance and report['remaining_attempt_directories']==0
