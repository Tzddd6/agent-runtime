"""Workflow-scoped Agent control blocks. Only the coordinator mutates ACBs."""
from dataclasses import dataclass,field
from collections import deque
import hashlib
import json
import queue
import re
import threading
import time
import uuid
from .telemetry import CgroupSampler

RESOURCES=('cpu_milli','memory_bytes','pids')


def agent_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value):
        raise ValueError('Agent ID must contain 1–64 ASCII letters, digits, underscores or hyphens')
    return value


class RuntimeControl:
    """Bounded asynchronous commands; HTTP never mutates live runtime state."""
    def __init__(self):
        self.lock=threading.Lock();self.queue=queue.Queue(64)
        self.known={};self.results=deque(maxlen=64);self.closed=False

    def request(self,identity,operation):
        agent_id(identity)
        if operation not in ('pause','resume','terminate'):raise ValueError('Unknown Agent operation')
        with self.lock:
            if self.closed:raise ValueError('工作流已结束，不能控制历史 Agent')
            if identity not in self.known:raise ValueError('Agent 尚未注册或不存在')
            if self.known[identity] in ('terminated','completed','failed'):
                raise ValueError('Agent 已终止或完成')
            command={'id':uuid.uuid4().hex,'agent_id':identity,'operation':operation,'status':'queued','time':time.time()}
            try:self.queue.put_nowait(command)
            except queue.Full:raise ValueError('Agent 控制队列已满') from None
            self.results.append(dict(command))
            return dict(command)

    def publish(self,snapshots):
        with self.lock:self.known={a['id']:a['state'] for a in snapshots}

    def acknowledge(self,command,error=None):
        with self.lock:
            for result in self.results:
                if result['id']==command['id']:
                    result.update(status='rejected' if error else 'applied',finished=time.time())
                    if error:result['error']=error

    def history(self):
        with self.lock:return [dict(r) for r in self.results]

    def close(self):
        with self.lock:
            self.closed=True
            for result in self.results:
                if result['status']=='queued':result.update(status='rejected',error='工作流已结束')


@dataclass
class AgentControlBlock:
    id:str
    role:str
    budget:dict
    max_concurrency:int=1
    state:str='registered'
    mode:str='open'
    tasks:set=field(default_factory=set)
    active:dict=field(default_factory=dict)
    context_refs:set=field(default_factory=set)
    reserved:dict=field(default_factory=lambda:{k:0 for k in RESOURCES})
    metrics:dict=field(default_factory=dict)
    model_calls:int=0
    token_usage:dict=field(default_factory=dict)
    created:float=field(default_factory=time.time)


class AgentRegistry:
    def __init__(self,budget,specs=None,*,cgroup_root=None,workflow_id='',journal=None):
        self.default=dict(budget);self.specs={};self.agents={};self.groups={}
        self.root=cgroup_root;self.workflow=workflow_id;self.journal=journal
        self.failed=False
        self.sampler=CgroupSampler();self.events=deque(maxlen=64);self.sequence=0
        if specs is not None and (not isinstance(specs,list) or len(specs)>100):raise ValueError('agents must be a bounded list')
        for spec in specs or []:
            if not isinstance(spec,dict) or set(spec)-{'id','role','budget','max_concurrency'}:raise ValueError('Invalid Agent specification')
            identity=agent_id(spec.get('id'))
            if identity in self.specs:raise ValueError('Duplicate Agent ID')
            if not isinstance(spec.get('budget',{}),dict):raise ValueError('Agent budget must be an object')
            limits={**budget,**spec.get('budget',{})}
            if set(limits)!=set(budget) or any(type(v) is not int or v<=0 or v>budget[k] for k,v in limits.items()):
                raise ValueError('Agent budget must fit workflow budget')
            slots=spec.get('max_concurrency',1);role=spec.get('role',identity)
            if type(slots) is not int or not 1<=slots<=4 or not isinstance(role,str) or len(role)>128:raise ValueError('Invalid Agent role/concurrency')
            self.specs[identity]={'budget':limits,'role':role,'max_concurrency':slots}

    def transition(self,acb,state,reason):
        if acb.state==state:return
        previous=acb.state;acb.state=state;self.sequence+=1
        event={'sequence':self.sequence,'time':time.time(),'agent_id':acb.id,'previous':previous,'state':state,'reason':reason}
        self.events.append(event)
        if self.journal:
            self.journal.write(json.dumps(event,ensure_ascii=False)+'\n');self.journal.flush()

    def register(self,identity):
        identity=agent_id(identity)
        if identity in self.agents:return self.agents[identity]
        if len(self.agents)>=100:raise ValueError('Agent registry exceeds 100 entries')
        spec=self.specs.get(identity,{'budget':dict(self.default),'role':identity,'max_concurrency':1})
        acb=AgentControlBlock(identity,spec['role'],dict(spec['budget']),spec['max_concurrency'])
        self.agents[identity]=acb
        if self.root:
            group=self.root/(self.workflow+'-agent-'+hashlib.sha256(identity.encode()).hexdigest()[:12])
            group.mkdir();self.groups[identity]=group
            for name,value in {'cpu.max':str(acb.budget['cpu_milli']*100)+' 100000',
                'memory.max':acb.budget['memory_bytes'],'memory.swap.max':0,'pids.max':acb.budget['pids']}.items():
                (group/name).write_text(str(value))
            (group/'cgroup.subtree_control').write_text('+cpu +memory +pids')
        self.transition(acb,'ready','registered')
        return acb

    def attach(self,task):
        acb=self.register(task.get('agent',task['id']));acb.tasks.add(task['id'])

    def blocked(self,identity):
        acb=self.agents[identity]
        return acb.mode!='open' or len(acb.active)>=acb.max_concurrency

    def fits(self,identity,limits,*,reserved=True):
        a=self.agents[identity]
        return all(limits[k]+(a.reserved[k] if reserved else 0)<=a.budget[k] for k in RESOURCES)

    def reserve(self,identity,key,pid,limits,refs):
        a=self.agents[identity]
        if self.blocked(identity) or not self.fits(identity,limits):raise RuntimeError('Agent admission invariant violated')
        a.active[key]=pid;a.context_refs.update(refs)
        for k in RESOURCES:a.reserved[k]+=limits[k]

    def release(self,identity,key,limits):
        a=self.agents[identity];a.active.pop(key)
        for k in RESOURCES:
            a.reserved[k]-=limits[k]
            if a.reserved[k]<0:raise RuntimeError('Agent reservation underflow')

    def synchronize(self,states,*,finished=False,failed=False):
        self.failed=self.failed or failed
        for a in self.agents.values():
            if a.mode=='terminated':state='terminated'
            elif a.mode=='terminating':state='terminating'
            elif finished:state='failed' if self.failed or any(states.get(k) in ('failed','timeout','unschedulable') for k in a.tasks) else 'completed'
            elif a.mode=='paused':state='pausing' if a.active else 'paused'
            elif a.active:state='running'
            elif any(states.get(k)=='waiting' for k in a.tasks):state='ready'
            else:state='idle'
            self.transition(a,state,'workflow_finished' if finished else 'coordinator')

    def snapshots(self):
        result=[]
        for a in self.agents.values():
            group=self.groups.get(a.id)
            if group and group.exists():a.metrics=self.sampler.sample(group)
            result.append({'id':a.id,'role':a.role,'state':a.state,'budget':dict(a.budget),
                'reserved':dict(a.reserved),'max_concurrency':a.max_concurrency,
                'tasks':sorted(a.tasks),'active_tasks':dict(a.active),'context_refs':sorted(a.context_refs),
                'mailbox_id':a.id,'metrics':dict(a.metrics),'created':a.created,'cgroup_path':str(group) if group else None,
                'model_calls':a.model_calls,'token_usage':dict(a.token_usage),
                'enforcement':'cgroup_v2' if group else 'admission_only'})
        return result

    def cleanup(self):
        errors=[]
        for identity,group in list(self.groups.items()):
            try:
                if 'populated 1' in (group/'cgroup.events').read_text():raise RuntimeError('Agent cgroup is still populated')
                self.agents[identity].metrics=self.sampler.sample(group)
                group.rmdir()
                self.agents[identity].metrics.update(cpu_cores=None,memory_bytes=0,pids=0)
            except (OSError,RuntimeError) as exc:errors.append(identity+': '+str(exc))
        if errors:raise RuntimeError('AGENT_CLEANUP_FAILED: '+'; '.join(errors))
