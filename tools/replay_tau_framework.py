"""Zero-provider-call replay through official orchestration, tools and grader.

Recorded synthetic model replies are fixtures, never supplied as hidden answers
to a live model. Compare unchanged requests, dialogue and rewards across code
versions. This measures framework overhead, not new model capabilities.
"""
import argparse,hashlib,json,os,resource,sys,time,subprocess
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class Replies:
    model='fixed-official-replay';timeout=20
    def __init__(self,simulation):
        self.actor='agent';self.requests=[]
        self.queues={'agent':[m for m in simulation.messages[1:] if m.role=='assistant'],
                     'simulator':[m for m in simulation.messages if m.role=='user']}
    def complete(self,messages,max_tokens):
        body=json.loads(messages[0]['content'].split(':',1)[1])
        self.requests.append({'actor':self.actor,'request_sha256':digest(body),'max_tokens':max_tokens})
        if not self.queues[self.actor]:raise AssertionError('Replay generated an unexpected extra request')
        message=self.queues[self.actor].pop(0)
        calls=[{'id':c.id,'type':'function','function':{'name':c.name,'arguments':json.dumps(c.arguments)}} for c in getattr(message,'tool_calls',None) or []]
        native={'role':'assistant','content':message.content}
        if calls:native['tool_calls']=calls
        return {'content':json.dumps({'message':native}), 'finish_reason':'tool_calls' if calls else 'stop',
                'model':self.model,'usage':{'prompt_tokens':1,'completion_tokens':1}}


def dialogue(sim):
    return [{'role':m.role,'content':m.content,'tool_calls':[c.model_dump(mode='json') for c in getattr(m,'tool_calls',None) or []],
             'tool_call_id':getattr(m,'id',None)} for m in sim.messages]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--trajectory',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--session',action='store_true')
    parser.add_argument('--direct',action='store_true');parser.add_argument('--limit',type=int)
    args=parser.parse_args()
    from tau2.data_model.simulation import Results,TextRunConfig
    from tau2.runner.batch import run_single_task
    from tau2.evaluator.evaluator import EvaluationType
    from agent_runtime.tau_adapter import TauBridge
    from agent_runtime.model_gateway import ModelGateway
    from loguru import logger
    logger.remove();logger.add(sys.stderr,level='ERROR')
    originals=Results.load(args.trajectory);tasks={t.id:t for t in originals.tasks}
    args.output.mkdir(parents=True,exist_ok=False)
    config=TextRunConfig(domain='airline',task_split_name='base',llm_agent='deepseek-flash',llm_user='deepseek-flash',
        llm_args_agent={'temperature':0.,'max_tokens':2048},llm_args_user={'temperature':0.,'max_tokens':2048},
        num_trials=1,max_concurrency=1,max_retries=0,hallucination_retries=0,auto_resume=False,auto_review=False,seed=300,max_steps=200)
    rows=[];start=time.monotonic();cpu=time.process_time();child=resource.getrusage(resource.RUSAGE_CHILDREN)
    for index,original in enumerate(originals.simulations[:args.limit]):
        client=Replies(original);folder=args.output/f'{index:03d}'
        opts={'reuse_workers':True} if args.session else {}
        bridge=TauBridge(client,folder,arm='direct' if args.direct else 'runtime',
            cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'),cny_limit=100000,max_calls=200,**opts)
        def generate(*a,**kw):
            client.actor='simulator' if kw.get('call_name')=='user_simulator_response' else 'agent'
            return bridge.generate(*a,**kw)
        import tau2.agent.llm_agent as agent
        import tau2.user.user_simulator as user
        stamp=time.monotonic()
        with patch.object(agent,'generate',generate),patch.object(user,'generate',generate),patch.object(ModelGateway,'from_env',side_effect=lambda log:ModelGateway(client,log)):
            try:result=run_single_task(config,tasks[original.task_id],seed=original.seed,evaluation_type=EvaluationType.ALL)
            finally:
                if hasattr(bridge,'close'):bridge.close()
        row={'task_id':original.task_id,'seconds':time.monotonic()-stamp,'reward':result.reward_info.reward,
            'expected_reward':original.reward_info.reward,'same_dialogue':digest(dialogue(result))==digest(dialogue(original)),
            'fixture_consumed':not any(client.queues.values()),'requests':client.requests}
        if args.session:row['session']=bridge.worker_session.stats
        rows.append(row);(args.output/'rows.json').write_text(json.dumps(rows,indent=2))
        if row['reward']!=row['expected_reward'] or not row['same_dialogue'] or not row['fixture_consumed']:
            raise AssertionError('Replay quality/protocol changed for task '+original.task_id)
        print('REPLAY',index,original.task_id,row['reward'],round(row['seconds'],3),flush=True)
    end_child=resource.getrusage(resource.RUSAGE_CHILDREN)
    report={'cases':len(rows),'all_identical':True,'supplier_requests':0,'seconds':time.monotonic()-start,
        'source':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        'coordinator_cpu_seconds':time.process_time()-cpu,
        'children_cpu_seconds':end_child.ru_utime+end_child.ru_stime-child.ru_utime-child.ru_stime,
        'coordinator_maxrss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'children_maxrss_kib':end_child.ru_maxrss,
        'request_sequence_sha256':digest([r['requests'] for r in rows]),
        'rewards':[r['reward'] for r in rows],'session':args.session,'direct':args.direct,
        'limits':['Fixed recorded replies: no new model-quality claim','CPU/timing exclude imports; RSS is not aggregate memory peak']}
    (args.output/'summary.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))


if __name__=='__main__':main()
