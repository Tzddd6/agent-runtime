"""Private coordinator checkpoints; never replay an uncertain side effect.

This is an at-most-once admission policy, not a distributed exactly-once claim.
Unfinished model calls/commands/writes block recovery. A crashed coordinator
also needs proof that the old delegated cgroup is no longer populated.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading

MAX_BYTES=16*1024**2
PURE_ACTIONS={'echo','sleep','clock','analyze_text','index_text','capabilities','load','fail'}
PURE_PLUGINS={'text.stats','math.calculate','python.syntax','json.validate','text.diff'}


class RecoveryBlocked(RuntimeError):
    pass


def read_checkpoint(directory,owner):
    directory=Path(directory)
    if directory.is_symlink() or (directory/'state.json').is_symlink():
        raise ValueError('Checkpoint symlinks refused')
    fd=os.open(directory/'state.json',os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as source:raw=source.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES:raise ValueError('Checkpoint exceeds bound')
    data=json.loads(raw)
    if data.get('schema')!=1 or data.get('owner')!=owner:
        raise PermissionError('Checkpoint belongs to another identity or schema')
    return data


def recovery_view(directory,owner):
    data=read_checkpoint(directory,owner)
    by_id={t['id']:t for t in data['tasks']}
    uncertain=[k for k,v in data['states'].items() if v=='running']
    blocked=[k for k in uncertain if not replay_safe(by_id[k])]
    paused=[k for k,v in data.get('agent_modes',{}).items() if v in ('paused','terminating')]
    unsupported_mailbox=any(t.get('action') in ('send','receive') for t in data['tasks']) and any(v in ('running','waiting') for v in data['states'].values())
    return {'available':True,'completed':sum(v=='succeeded' for v in data['states'].values()),
            'unfinished':sum(v in ('running','waiting') for v in data['states'].values()),
            'uncertain_tasks':uncertain,'blocked_tasks':blocked,'paused_agents':paused,
            'mailbox_reconciliation_required':unsupported_mailbox,
            'clean_shutdown':data.get('clean_shutdown',False),
            'automatic_model_replay':False,'model_budget_preserved':bool(data.get('model_ledger')),
            'policy':'confirmed results retained; uncertain model/tool side effects require reconciliation'}


def replay_safe(task):
    action=task.get('action','echo')
    return action in PURE_ACTIONS or action=='plugin' and task.get('plugin') in PURE_PLUGINS


class WorkflowCheckpoint:
    def __init__(self,directory,owner,specification,*,resume=False):
        if os.name!='posix':raise ValueError('Durable execution currently requires POSIX file locks')
        if not isinstance(owner,str) or not 1<=len(owner)<=128:raise ValueError('Invalid recovery identity')
        import fcntl
        self.directory=Path(directory).absolute()
        if self.directory.is_symlink():raise ValueError('Checkpoint symlinks refused')
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.directory.chmod(0o700)
        self.lock=threading.RLock();self.lease=None;self.data=None
        fd=os.open(self.directory/'lease',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        self.lease=os.fdopen(fd,'r+')
        try:
            fcntl.flock(self.lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
            raw=json.dumps(specification,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()
            signature=hashlib.sha256(raw).hexdigest()
            if resume:
                self.data=read_checkpoint(self.directory,owner)
                if self.data['specification_sha256']!=signature:raise ValueError('Recovery task/config/workspace mismatch')
                view=recovery_view(self.directory,owner);blocked=view['blocked_tasks']
                if blocked:raise RecoveryBlocked('Uncertain side effects; no automatic replay: '+','.join(blocked))
                if view['paused_agents'] or view['mailbox_reconciliation_required']:
                    raise RecoveryBlocked('Paused Agent or mailbox needs explicit reconciliation')
                if not self.data.get('clean_shutdown'):
                    group=self.data.get('cgroup_root')
                    if not group or not str(group).startswith('/sys/fs/cgroup/'):
                        raise RecoveryBlocked('Crashed execution needs verified old cgroup cleanup')
                    try:populated='populated 1' in (Path(group)/'cgroup.events').read_text()
                    except FileNotFoundError:populated=False
                    if populated:raise RecoveryBlocked('Old cgroup is still populated; no duplicate execution')
                self.data['states']={k:('waiting' if v=='running' else v) for k,v in self.data['states'].items()}
                self.data['resumes']=self.data.get('resumes',0)+1
            else:
                if (self.directory/'state.json').exists():raise ValueError('Checkpoint already exists; explicit resume required')
                self.data={'schema':1,'owner':owner,'specification':specification,
                           'specification_sha256':signature,'resumes':0}
        except BaseException:self.close();raise

    def write(self):
        raw=json.dumps(self.data,ensure_ascii=False,allow_nan=False).encode()
        if len(raw)>MAX_BYTES:raise ValueError('Checkpoint exceeds bound; execution refused')
        target=self.directory/'state.json';temporary=self.directory/('.state-'+secrets.token_hex(8))
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,target)
            fd=os.open(self.directory,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        finally:temporary.unlink(missing_ok=True)

    def record(self,*,root,tasks,states,attempts,events,outputs,store,cgroup_root,agent_modes,model_ledger):
        with self.lock:
            self.data.update(copy.deepcopy(dict(root=str(root),tasks=tasks,states=dict(states),attempts=dict(attempts),
                             events=events,outputs=dict(outputs),clean_shutdown=False,
                             cgroup_root=str(cgroup_root) if cgroup_root else None,
                             grants={k:sorted(v) for k,v in store.grants.items()},sources=store.sources,agent_modes=agent_modes,
                             model_ledger=model_ledger)))
            self.write()

    def model_admission(self,budget,used,agents):
        with self.lock:
            self.data['model_ledger']=copy.deepcopy({'budget':budget,'used':used,'agents':agents})
            self.write()  # Durable before any real provider submission.

    def clean(self):
        with self.lock:self.data['clean_shutdown']=True;self.write()

    def close(self):
        if self.lease is not None:self.lease.close();self.lease=None
