"""tau2 v1.0.1 native message bridge and measured Runtime arm.

No hidden task/actions/rewards are accepted here. Official tools execute in the
official environment; only model turns enter Runtime Attempts. The official
orchestrator/evaluator stay unchanged. Simulator model changes are declared.
"""
import json
import threading
import time
import uuid
import os
import math
from contextlib import contextmanager
from pathlib import Path
from .benchmark_transport import encode
from .runtime import run


class EvaluationBudget:
    """Conservative peak-rate reservation, including uncertain failed calls.

    Input reservation uses the full 1M-token provider context ceiling rather
    than assuming a tokenizer estimate. Price snapshot: 2026-11-17 official
    Flash rates. Known costs conservatively use peak rates even off-peak.
    """
    def __init__(self, cny, max_calls, ledger=None):
        if type(cny) not in (int,float) or not math.isfinite(cny) or cny<=0 or type(max_calls) is not int or max_calls<1:
            raise ValueError('Finite positive budget required')
        self.limit=cny;self.max_calls=max_calls;self.spent=0.;self.calls=0;self.lock=threading.Lock()
        self.ledger=Path(ledger) if ledger else None

    @contextmanager
    def transaction(self):
        with self.lock:
            if self.ledger is None:
                yield
                return
            import fcntl
            self.ledger.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            if self.ledger.is_symlink():raise ValueError('Symlink budget ledger refused')
            lock_path=self.ledger.with_suffix('.lock')
            descriptor=os.open(lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
            with os.fdopen(descriptor,'r+') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                if self.ledger.exists():
                    state=json.loads(self.ledger.read_text())
                    if state['limit_cny']!=self.limit:raise ValueError('Shared evaluation budget limit mismatch')
                    self.spent=state['spent_or_reserved_cny'];self.calls=state['calls']
                    if (type(self.spent) not in (int,float) or not math.isfinite(self.spent)
                            or self.spent<0 or self.spent>self.limit+1e-8 or type(self.calls) is not int or self.calls<0):
                        raise ValueError('Invalid budget ledger; refusing new calls')
                yield
                temporary=self.ledger.with_name(self.ledger.name+'.'+uuid.uuid4().hex)
                descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(descriptor,'w') as stream:
                    json.dump({'limit_cny':self.limit,'spent_or_reserved_cny':self.spent,'calls':self.calls},stream)
                    stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,self.ledger)
                parent=os.open(self.ledger.parent,os.O_DIRECTORY)
                try:os.fsync(parent)
                finally:os.close(parent)

    def reserve(self, output_tokens):
        reserve=(1048576*2.+output_tokens*8.)/1000000
        with self.transaction():
            if self.calls>=self.max_calls or self.spent+reserve>self.limit:
                raise RuntimeError('Evaluation budget exhausted; no new provider request')
            self.calls+=1;self.spent+=reserve
        return reserve

    def settle(self, reserve, usage):
        prompt=usage.get('prompt_tokens');output=usage.get('completion_tokens')
        if type(prompt) is not int or type(output) is not int or not 0<=prompt<=1048576 or not 0<=output<=4096:return None
        hit=usage.get('prompt_cache_hit_tokens',0)
        if type(hit) is not int or not 0<=hit<=prompt:hit=0
        cost=((prompt-hit)*2.+hit*.04+output*8.)/1000000
        with self.transaction():self.spent+=max(0.,cost)-reserve
        return cost


class TauBridge:
    def __init__(self, client, output, *, arm='runtime', cgroup_root=None,
                 cny_limit=10.,max_calls=32,ledger=None,reuse_workers=False):
        if arm not in ('runtime','direct'):raise ValueError('Invalid arm')
        self.client=client;self.output=Path(output);self.output.mkdir(parents=True,exist_ok=True)
        self.arm=arm;self.cgroup_root=cgroup_root;self.budget=EvaluationBudget(cny_limit,max_calls,ledger)
        self.records=[];self.lock=threading.Lock()
        self.worker_session=None
        if reuse_workers and arm=='runtime':
            from .worker_session import WorkerSession
            self.worker_session=WorkerSession(self.output)
        self.budget_exhausted=False

    def generate(self,model,messages,tools=None,tool_choice=None,call_name=None,**kwargs):
        from tau2.utils.llm_utils import to_litellm_messages,validate_message_history
        from tau2.data_model.message import AssistantMessage,ToolCall
        validate_message_history(messages)
        wire=encode(to_litellm_messages(messages),[tool.openai_schema for tool in tools] if tools else None,
                    temperature=kwargs.get('temperature',0.0),seed=kwargs.get('seed'),tool_choice=tool_choice)
        tokens=kwargs.get('max_tokens',2048)
        if type(tokens) is not int or not 1<=tokens<=4096:raise ValueError('Unsupported token allowance')
        try:reserve=self.budget.reserve(tokens)
        except RuntimeError:
            self.budget_exhausted=True
            raise
        start=time.monotonic();key=uuid.uuid4().hex
        record={'id':key,'actor':'simulator' if call_name=='user_simulator_response' else 'agent' if call_name=='agent_response' else 'judge',
                'arm':self.arm,'call_name':call_name,'status':'failed','reserved_peak_cny':reserve}
        try:
            if self.arm=='runtime' and record['actor']=='agent':
                states,events=run([{'id':'tau_turn','agent':'tau-agent','action':'llm','messages':wire,
                    'max_tokens':tokens,'timeout':65,'resources':{'cpu_milli':500,'memory_bytes':128*1024**2,'pids':16}}],
                    max_workers=1,quiet=True,output_dir=self.output/key,cgroup_root=self.cgroup_root,
                    budget={'cpu_milli':2000,'memory_bytes':1024**3,'pids':128},worker_session=self.worker_session)
                if states['tau_turn']!='succeeded':raise RuntimeError('Runtime inference turn failed; no replay')
                event=next(e for e in events if e['task_id']=='tau_turn' and e['state']=='succeeded')
                result=event['result']['model_call'];record['runtime_usage']=event.get('usage',{})
            else:
                result=self.client.complete(wire,tokens)
            if result.get('finish_reason')=='length':raise RuntimeError('Incomplete benchmark response; no replay')
            native=json.loads(result['content'])['message']
            calls=[ToolCall(id=c['id'],name=c['function']['name'],arguments=json.loads(c['function']['arguments'])) for c in native.get('tool_calls') or []]
            usage=result.get('usage') or {};cost=self.budget.settle(reserve,usage)
            record.update(status='succeeded',usage=usage,conservative_peak_cost_cny=cost,
                          provider_model=result.get('model'),tool_calls=len(calls))
            return AssistantMessage(role='assistant',content=native.get('content'),tool_calls=calls or None,
                cost=cost,usage=usage,generation_time_seconds=time.monotonic()-start,
                raw_data={'model':result.get('model'),'finish_reason':result.get('finish_reason'),'transport':'native_protected_broker'})
        finally:
            record['seconds']=time.monotonic()-start
            with self.lock:
                self.records.append(record)
                with (self.output/'calls.jsonl').open('a') as stream:stream.write(json.dumps(record)+'\n')

    def close(self):
        if self.worker_session is not None:self.worker_session.close()

    def install(self):
        # Keep official Agent/User prompts and message/state conversion. Replace
        # only their imported model transport function, in this isolated process.
        import tau2.agent.llm_agent as agent
        import tau2.user.user_simulator as user
        agent.generate=self.generate;user.generate=self.generate
