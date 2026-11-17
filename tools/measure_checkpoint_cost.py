"""Fixed replies: separately measure optional persistence, without a provider."""
import argparse,hashlib,json,os,resource,sys,time
from pathlib import Path


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--durable',action='store_true')
    args=parser.parse_args()
    from agent_runtime.runtime import run
    from agent_runtime.worker_session import WorkerSession
    class Client:
        model='fixed-checkpoint-fixture';timeout=3
        def __init__(self):self.requests=[]
        def complete(self,messages,max_tokens):
            self.requests.append({'messages':messages,'max_tokens':max_tokens})
            return {'content':'fixed result','finish_reason':'stop','usage':{'prompt_tokens':4,'completion_tokens':2}}
    client=Client();tasks=[{'id':f't{i}','agent':'same','action':'llm','messages':[{'role':'user','content':'fixed request'}],
                          'max_tokens':16,'resources':{'cpu_milli':1000,'memory_bytes':128*1024**2,'pids':16}} for i in range(12)]
    args.output.mkdir(parents=True,exist_ok=False);options={}
    if args.durable:options={'checkpoint_dir':args.output/'checkpoint','recovery_owner':'fixture-owner'}
    start=time.monotonic();cpu=time.process_time();child=resource.getrusage(resource.RUSAGE_CHILDREN)
    with WorkerSession(args.output) as session:
        states,events=run(tasks,1,quiet=True,output_dir=args.output,cgroup_root=os.environ['AGENTRT_TEST_CGROUP'],
                         worker_session=session,model_client=client,**options)
    values=[e['result']['model_call']['content'] for e in events if e['state']=='succeeded']
    assert all(s=='succeeded' for s in states.values()) and values==['fixed result']*12 and len(client.requests)==12
    report={'seconds':time.monotonic()-start,'coordinator_cpu':time.process_time()-cpu,
            'child_cpu':(resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime+resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime-child.ru_utime-child.ru_stime),
            'requests_sha256':hashlib.sha256(json.dumps(client.requests,sort_keys=True).encode()).hexdigest(),
            'results_sha256':hashlib.sha256(json.dumps(values).encode()).hexdigest(),
            'tasks':12,'fixed_model_callbacks':12,'supplier_requests':0,'durable':args.durable}
    (args.output/'measurement.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))


if __name__=='__main__':main()
