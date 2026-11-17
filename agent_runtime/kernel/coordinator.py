"""Classified admission queues sharing one coordinator and resource envelope.

No new thread or process per metadata syscall. Round-robin rotates *admission*
across services; it cannot interrupt/restart a remote provider generation.
"""
import time
from .syscall import SERVICES,Syscall,service_for


class KernelCoordinator:
    def __init__(self,workflow_id,*,owner='local',queue_policy='fifo'):
        if not isinstance(owner,str) or not 1<=len(owner)<=128:raise ValueError('Invalid trusted kernel owner')
        if queue_policy not in ('fifo','round_robin'):raise ValueError('Invalid kernel queue policy')
        self.workflow_id=workflow_id;self.owner=owner;self.queue_policy=queue_policy
        self.calls={};self.ready_since={};self.cursor=0;self.selections=0

    def choose(self,scheduler,ready,used,**options):
        ready=list(ready);now=time.monotonic();ids={t['id'] for t in ready}
        self.ready_since={k:v for k,v in self.ready_since.items() if k in ids}
        for task in ready:self.ready_since.setdefault(task['id'],now)
        # A requested global FIFO remains global FIFO. Age and DRF accounting
        # still belong to AdmissionScheduler, including their drain decisions.
        if self.queue_policy=='round_robin' and scheduler.policy!='fifo':
            buckets={kind:[] for kind in SERVICES}
            for task in ready:buckets[service_for(task)].append(task)
            ready=[task for index in range(len(SERVICES))
                   for task in buckets[SERVICES[(self.cursor+index)%len(SERVICES)]]]
        self.selections+=1
        return scheduler.choose(ready,used,now=now,**options)

    def dispatch(self,task,attempt):
        key=(task['id'],attempt)
        if key in self.calls:raise RuntimeError('Duplicate syscall dispatch refused')
        kind=service_for(task);now=time.monotonic()
        if task.get('action')=='plugin':
            from ..capabilities import PLUGINS
            operation=task.get('plugin')
            operation=operation if isinstance(operation,str) and operation in PLUGINS else 'invalid_plugin'
        else:
            operation=task.get('action','echo')
            if not isinstance(operation,str) or len(operation)>64:operation='invalid_action'
        waited=max(0.,now-self.ready_since.pop(task['id'],now))
        call=Syscall(self.workflow_id+':'+task['id']+':'+str(attempt),task['id'],
                     task.get('agent',task['id']),kind,
                     operation,
                     attempt,time.time()-waited,wait_seconds=waited)
        self.calls[key]=call
        if self.queue_policy=='round_robin':self.cursor=(SERVICES.index(kind)+1)%len(SERVICES)
        return {'id':call.id,'service':kind,'operation':call.operation,'attempt':attempt}

    def event(self,task,state,attempt,*,pid=None,stamp=None):
        call=self.calls.get((task['id'],attempt))
        if call is None:return None # dependency cancellation need not have an Attempt.
        stamp=time.time() if stamp is None else stamp
        if state=='running':call.started=stamp;call.pid=pid;call.state='running'
        elif state in ('succeeded','failed','timeout','cancelled','waiting'):
            call.finished=stamp;call.state='retrying' if state=='waiting' else state
        return {'id':call.id,'service':call.service,'operation':call.operation,'attempt':attempt}

    def restore(self,events,tasks):
        by_id={t['id']:t for t in tasks}
        for event in events:
            attempt=event.get('attempt',0);task=by_id.get(event.get('task_id'))
            if not task or not attempt:continue
            if (task['id'],attempt) not in self.calls:
                self.dispatch(task,attempt)
                self.calls[(task['id'],attempt)].created=event.get('time',time.time())
            self.event(task,event['state'],attempt,pid=event.get('pid'),stamp=event.get('time'))

    def snapshot(self,tasks,states,*,limit=100):
        queues={kind:{'waiting':0,'ready':0,'running':0,'finished':0,'failed':0} for kind in SERVICES}
        for task in tasks:
            kind=service_for(task);state=states[task['id']]
            if state=='waiting':
                queues[kind]['waiting']+=1
                if all(states.get(d)=='succeeded' for d in task.get('depends_on',[])):queues[kind]['ready']+=1
            elif state=='running':queues[kind]['running']+=1
        calls=list(self.calls.values())
        for call in calls:
            if call.finished is not None:
                queues[call.service]['finished']+=1
                queues[call.service]['failed']+=call.state in ('failed','timeout','cancelled','retrying')
        now=time.time()
        return {'schema':1,'architecture':'scoped_service_kernel','queue_policy':self.queue_policy,
                'scope':'workflow','trusted_owner_bound':True,'remote_model_preemption':False,
                'queues':queues,'selections':self.selections,'total_calls':len(calls),
                'calls':[call.public(now) for call in calls[-limit:]],'calls_truncated':len(calls)>limit}
