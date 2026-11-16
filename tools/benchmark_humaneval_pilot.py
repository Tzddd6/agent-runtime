"""Official pinned HumanEval smoke subset; no claim of a full benchmark score."""
import argparse,json,os,sys,time,statistics
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from agent_runtime.model_broker import BrokerClient
from agent_runtime.model_gateway import ModelGateway
from agent_runtime.runtime import run
from agent_runtime.web import delegated_attempts

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--run',type=Path,default=ROOT/'reports/humaneval-fresh');args=parser.parse_args()
 data=ROOT/'validation/humaneval-pilot';folder=args.run;folder.mkdir(mode=0o700,parents=True,exist_ok=False)
 rows=json.loads((data/'dataset.json').read_text());client=BrokerClient.from_env();attempts=delegated_attempts();results=[]
 assert not client.status()['busy']
 for index,case in enumerate(rows):
  for arm in (['direct','runtime'] if index%2==0 else ['runtime','direct']):
   if any(r['task_id']==case['task_id'] and r['arm']==arm for r in results):continue
   client=BrokerClient.from_env()
   sample=folder/(str(index)+'-'+arm);sample.mkdir();workspace=sample/'workspace';workspace.mkdir()
   messages=[{'role':'system','content':'Return only complete Python source implementing the requested function, including any needed imports. No Markdown or explanations.'},{'role':'user','content':case['prompt']}]
   (sample/'intent.json').write_text(json.dumps({'task_id':case['task_id'],'arm':arm,'max_tokens':2048}))
   start=time.monotonic()
   if arm=='direct':value=client.complete(messages,2048);text=value['content'];usage=value.get('usage',{});finish=value.get('finish_reason')
   else:
    task={'id':'candidate','action':'llm','messages':messages,'max_tokens':2048,'timeout':65}
    with patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda p:ModelGateway(client,p)):
     states,events=run([task],quiet=True,cgroup_root=attempts,output_dir=sample/'generation',model_budget={'calls':1,'output_tokens':2048},budget={'cpu_milli':2000,'memory_bytes':1024**3,'pids':128})
    if states['candidate']!='succeeded':raise RuntimeError('Model failure; do not replay')
    value=next(e['result']['model_call'] for e in events if e['state']=='succeeded');text=value['content'];usage=value.get('usage',{});finish=value.get('finish_reason')
   model_seconds=time.monotonic()-start
   if text.strip().startswith('```') and text.strip().endswith('```'):text='\n'.join(text.strip().splitlines()[1:-1])
   (workspace/'check.py').write_text(text+'\n'+case['test']+'\ncheck('+case['entry_point']+')\n')
   states,events=run([{'id':'judge','action':'plugin','plugin':'command.run','arguments':{'argv':['python3','-B','check.py'],'timeout':10}}],quiet=True,output_dir=sample/'judge',workspace=workspace,cgroup_root=attempts)
   judged=next((e['result']['result'] for e in events if e['state']=='succeeded'),{})
   row={'task_id':case['task_id'],'arm':arm,'passed':finish=='stop' and judged.get('exit_code')==0,'model_seconds':model_seconds,'total_seconds':time.monotonic()-start,'usage':usage,'calls':1}
   results.append(row);(folder/'results.json').write_text(json.dumps(results,indent=2));print(json.dumps(row),flush=True)
 summary={arm:{'passed':sum(r['passed'] for r in results if r['arm']==arm),'samples':5,'calls':5,'median_seconds':statistics.median(r['total_seconds'] for r in results if r['arm']==arm),'tokens':sum(r['usage'].get('total_tokens',0) for r in results if r['arm']==arm)} for arm in ['direct','runtime']}
 (folder/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary),flush=True)
if __name__=='__main__':main()
