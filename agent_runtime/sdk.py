"""Small typed SDK for the scoped Runtime kernel (not Cerebrum wire compatible).

Calls compile to normal Tasks, then traverse the real scheduler, fresh Worker,
resource accounting and service managers. No bypass or implicit retry exists.
The trusted embedding application binds user identity, not model arguments.
"""
from dataclasses import dataclass,field
import json
import hashlib
from pathlib import Path
from .agents import agent_id


def snapshot(value):
    raw=json.dumps(value,ensure_ascii=False,allow_nan=False)
    if len(raw.encode())>1024**2:raise ValueError('SDK query exceeds 1 MiB')
    return json.loads(raw)


@dataclass(frozen=True)
class LLMQuery:
    messages:list
    max_tokens:int=1024
    thinking:bool=False
    def task(self):
        if type(self.max_tokens) is not int or not 1<=self.max_tokens<=4096 or type(self.thinking) is not bool:
            raise ValueError('Invalid model output allowance or thinking option')
        messages=snapshot(self.messages)
        if not isinstance(messages,list) or not messages or any(not isinstance(m,dict) or set(m)!={'role','content'}
            or m['role'] not in ('system','user','assistant') or not isinstance(m['content'],str) for m in messages):
            raise ValueError('SDK LLMQuery requires role/content text messages')
        return {'action':'llm','messages':messages,'max_tokens':self.max_tokens,'thinking':self.thinking}


@dataclass(frozen=True)
class ToolQuery:
    name:str
    arguments:dict=field(default_factory=dict)
    def task(self):
        from .capabilities import PLUGINS
        if not isinstance(self.name,str) or self.name not in PLUGINS or not isinstance(self.arguments,dict):
            raise ValueError('Registered tool and object arguments required')
        # Identity and bearer tokens cannot be smuggled into the task envelope.
        if set(self.arguments)&{'user_id','agent_id','_gateway_token','_ipc_token'}:raise ValueError('Identity is trusted-client bound')
        return {'action':'plugin','plugin':self.name,'arguments':snapshot(self.arguments)}


@dataclass(frozen=True)
class MemoryQuery:
    operation:str
    params:dict=field(default_factory=dict)
    def task(self):
        allowed={'write':{'content','policy'},'read':{'budget'},'delete':{'memory_id'}}
        if not isinstance(self.operation,str) or self.operation not in allowed or not isinstance(self.params,dict) or set(self.params)-allowed[self.operation]:
            raise ValueError('Invalid memory operation; identity is capability-bound')
        return {'action':{'write':'memory_write','read':'memory_read','delete':'memory_delete'}[self.operation],**snapshot(self.params)}


@dataclass(frozen=True)
class StorageQuery:
    operation:str
    params:dict=field(default_factory=dict)
    def task(self):
        if not isinstance(self.operation,str) or self.operation not in ('read','write','list','copy','replace'):raise ValueError('Invalid storage operation')
        return ToolQuery('file.'+self.operation,self.params).task()


@dataclass(frozen=True)
class Request:
    id:str
    agent:str
    query:object
    depends_on:tuple=()
    inputs:tuple=()
    contexts:tuple=()
    timeout:float=65
    resources:dict=field(default_factory=dict)
    def task(self):
        if not isinstance(self.query,(LLMQuery,ToolQuery,MemoryQuery,StorageQuery)):raise ValueError('Typed kernel query required')
        agent_id(self.agent)
        return {**self.query.task(),'id':self.id,'agent':self.agent,'depends_on':list(self.depends_on),
                'inputs':list(self.inputs),'contexts':snapshot(list(self.contexts)),
                'timeout':self.timeout,'resources':snapshot(self.resources)}


class KernelClient:
    def __init__(self,root,*,user_identity='local',**options):
        if not isinstance(user_identity,str) or not 1<=len(user_identity)<=128:raise ValueError('Invalid trusted identity')
        if set(options)&{'output_dir','workspace','recovery_owner','_kernel_owner','_checkpoint'}:raise ValueError('Kernel client controls its workspace and identity')
        base=Path(root).resolve()
        self.root=base/'users'/hashlib.sha256(user_identity.encode()).hexdigest()
        if self.root.is_symlink():raise ValueError('Symlink SDK user root refused')
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        repository=options.get('memory_repository')
        if repository is not None and getattr(repository,'user',None)!=user_identity:raise ValueError('Memory repository belongs to another trusted user')
        self.user=user_identity;self.options=dict(options)

    def batch(self,requests,*,max_workers=2):
        requests=list(requests)
        if not 1<=len(requests)<=100 or any(not isinstance(r,Request) for r in requests):raise ValueError('A bounded typed request batch is required')
        tasks=[r.task() for r in requests]
        # Scope the acceptance barrier to this typed batch. Dependencies carry
        # ordering only, never another Agent's private memory contents. Failed
        # writes skip later reads/models rather than silently using stale data.
        writes=[]
        for task in tasks:
            action=task['action'];plugin=task.get('plugin')
            if action in ('llm','memory_read') or plugin=='memory.recall':
                task['depends_on']=list(dict.fromkeys(task['depends_on']+writes))
            if action in ('memory_write','memory_delete') or plugin in ('memory.remember','memory.forget'):
                writes.append(task['id'])
        from .runtime import run
        options={'kernel_queue_policy':'round_robin','quiet':True,**self.options}
        states,events=run(tasks,max_workers=max_workers,output_dir=self.root/'runs',workspace=self.root/'workspace',
                          recovery_owner=self.user,**options)
        responses={event['task_id']:event['result']['result'] for event in events
                   if event['state']=='succeeded' and isinstance(event.get('result'),dict)}
        return {'states':states,'responses':responses,'events':events,
                'syscalls':[event['syscall'] for event in events if event.get('syscall') and event['state']!='running']}

    def execute_request(self,agent_name,query):
        result=self.batch([Request('request',agent_name,query)],max_workers=1)
        return {**result,'response':result['responses'].get('request')}
