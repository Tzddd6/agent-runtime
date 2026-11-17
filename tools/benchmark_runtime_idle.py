"""Free controlled 1-second model wait, identical replies, isolated service arms."""
import json,os,resource,sys,tempfile,time
from pathlib import Path
root=Path(sys.argv[1]);out=Path(sys.argv[2]);sys.path.insert(0,str(root));os.chdir(root)
from agent_runtime.runtime import run
from agent_runtime.worker_session import WorkerSession
from agent_runtime.web import delegated_attempts
class Client:
 model='fixed';timeout=2
 def complete(self,*args):time.sleep(1);return {'content':'fixed result','finish_reason':'stop','usage':{'prompt_tokens':10,'completion_tokens':10}}
folder=out.parent/(out.stem+'-private-runs');folder.mkdir(mode=0o700,exist_ok=False)
group=delegated_attempts();rows=[]
with WorkerSession(folder) as session:
 for i in range(3):
  start=time.monotonic();cpu=time.process_time();states,events=run([{'id':'model','action':'llm','messages':[{'role':'user','content':'fixed'}],'max_tokens':16,'timeout':5}],quiet=True,output_dir=folder,worker_session=session,model_client=Client(),cgroup_root=group)
  assert states['model']=='succeeded';assert next(e['result']['result'] for e in events if e['state']=='succeeded')=='fixed result'
  summary=json.loads(max(folder.glob('*/summary.json'),key=lambda p:p.stat().st_mtime).read_text())
  rows.append({'seconds':time.monotonic()-start,'coordinator_cpu_seconds':time.process_time()-cpu,'iterations':summary['scheduling_stats']['iterations'],'waits':summary['scheduling_stats']['waits'],'exit_wakeups':summary['scheduling_stats']['exit_wakeups']})
out.write_text(json.dumps({'rows':rows,'supplier_calls':0,'all_replies_identical':True,'fixed_provider_seconds':1},indent=2)+'\n');print(json.dumps({'output':out.name,'rows':rows}))
