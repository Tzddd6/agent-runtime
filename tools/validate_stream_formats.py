"""Four bounded synthetic format probes; never replay failures or export text."""
import json,time,hashlib,argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_runtime.model_broker import BrokerClient
from agent_runtime.runtime import run
from agent_runtime.worker_session import WorkerSession
from agent_runtime.web import delegated_attempts
from tools.benchmark_provider_paired import Ledger

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--live',action='store_true');parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
 if not args.live:raise SystemExit('Explicit --live required; no provider request made')
 root=args.output;root.mkdir(mode=0o700,parents=True,exist_ok=False)
 c=BrokerClient.from_env();assert c.status()['busy']==0
 ready=[p for p in c.profiles_info()['profiles'] if p['provider']=='aizex' and p['model']=='gpt-6-luna' and p['configured']];assert len(ready)==1
 client=c.for_profile(ready[0]['id'],'gpt-6-luna');client.supports_stream_callbacks=True
 cases=[('mixed','**流式测试**\n\n| 项目 | 结果 |\n| --- | --- |\n| A | 1 |\n\n$$x^2$$\n\n```python\nprint(1)\n```',256),('formula',r'## 公式'+ '\n\n'+r'\[F(s)=\int_0^{\infty}f(t)e^{-st}\,dt\]'+'\n\n'+r'\(x=\frac{1}{2}\), $a^2+b^2=c^2$.'+'\n\n'+r'$$\begin{aligned}x+y&=3\\x-y&=1\end{aligned}$$',512),('code','## 代码\n\n```python\ndef greet(name):\n    return f"你好，{name}\\n"\n\nprint(greet("Runtime"))\n```\n\n```json\n{"items": [1, 2], "ok": true}\n```',512),('long_table','## 长表格\n\n| 序号 | 数值 |\n| --- | --- |\n'+'\n'.join('| '+str(i)+' | '+str(i*i)+' |' for i in range(1,41))+'\n\n**完成。**',1536)]
 from agent_runtime.transport_trace import MARKER
 marker=MARKER;assert not marker.exists();marker.write_text(json.dumps({'expires_at':time.time()+900}));marker.chmod(0o600);log=marker.parent/'transport-phases.jsonl'
 ledger=Ledger(root/'ledger.json',limit=.003,input_price=.1,output_price=.5,price_source='User supplied relay nominal rates; actual billing unverified');group=delegated_attempts();rows=[]
 try:
  with WorkerSession(root/'runs') as session:
   for case,target,limit in cases:
    messages=[{'role':'system','content':'Repeat the requested text exactly without a preface. Preserve every backslash, newline, Markdown marker and table row.'},{'role':'user','content':target}]
    entry=ledger.admit(messages,limit);offset=log.stat().st_size if log.exists() else 0;updates=[];start=time.monotonic()
    def observe(e):
     if isinstance(e.get('text'),str):updates.append((time.monotonic()-start,e['text']))
    states,events=run([{'id':'assistant','action':'llm','messages':messages,'max_tokens':limit,'timeout':120}],quiet=True,output_dir=root/'runs',worker_session=session,model_client=client,cgroup_root=group,output_observer=observe,model_budget={'calls':1,'output_tokens':limit})
    row={'case':case,'state':states['assistant'],'seconds':time.monotonic()-start,'progress_events':len(updates),'first_event_seconds':updates[0][0] if updates else None,'passed':False}
    if states['assistant']=='succeeded':
     result=next(e['result']['model_call'] for e in events if e['state']=='succeeded');text=result['content'];entry.update(state='succeeded',usage=result['usage']);row.update(usage=result['usage'],stream_equals_final=bool(updates) and updates[-1][1]==text,exact_text=text.strip()==target.strip(),final_sha256=hashlib.sha256(text.encode()).hexdigest());row['passed']=row['stream_equals_final'] and row['exact_text']
    else:entry.update(state='failed_billing_unknown');row['billing_unknown']=True
    ledger.flush()
    if log.exists():
     with log.open() as f:f.seek(offset);row['transport_phases']=[json.loads(line) for line in f if line.strip()]
    rows.append(row);report={'rows':rows,'attempts':len(rows),'passed':sum(r['passed'] for r in rows),'reserved_nominal_usd':ledger.reserved,'automatic_retry':False,'model':'gpt-6-luna'};(root/'summary.json').write_text(json.dumps(report,indent=2));print(json.dumps(row),flush=True)
 finally:marker.unlink(missing_ok=True)
if __name__=='__main__':main()
