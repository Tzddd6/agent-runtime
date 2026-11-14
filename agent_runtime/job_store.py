"""Atomic, bounded console index. This restores observation, never execution."""
import heapq
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading


FIELDS={'id','preset','project','status','created','finished','workers','policy','budget',
        'error','final_states','history','monitor','agent_summary','agent_control_summary','model_budget','model_resource_summary'}
MAX_BYTES=512*1024


class JobStore:
    def __init__(self,root):self.root=Path(root)

    def save(self,job):
        key=job['id']
        if not re.fullmatch('[a-f0-9]{32}',key):raise ValueError('Invalid job identity')
        value={k:job[k] for k in FIELDS if k in job}
        value['schema']=1
        raw=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        if len(raw)>MAX_BYTES:raise ValueError('Console index exceeds bound')
        directory=self.root/key
        directory.mkdir(exist_ok=True)
        if directory.is_symlink():raise ValueError('Symlink job directory refused')
        target=directory/'job.json'
        temporary=directory/('.job-'+secrets.token_hex(8))
        try:
            fd=os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,target)
        finally:temporary.unlink(missing_ok=True)

    def load(self,limit=20):
        candidates=[]
        for item in self.root.iterdir():
            if not re.fullmatch('[a-f0-9]{32}',item.name) or item.is_symlink() or not item.is_dir():continue
            try:
                path=item/'job.json'
                if path.is_symlink() or path.stat().st_size>MAX_BYTES:continue
                with path.open('rb') as source:raw=source.read(MAX_BYTES+1)
                if len(raw)>MAX_BYTES:continue
                data=json.loads(raw)
                if (data.get('schema')!=1 or data.get('id')!=item.name or
                        data.get('status') not in ('running','cancelling','succeeded','failed','cancelled','interrupted','recovery_required') or
                        type(data.get('created')) not in (int,float) or not math.isfinite(data['created'])):
                    continue
                value={k:data[k] for k in FIELDS if k in data}
                if value['status'] in ('running','cancelling'):
                    value['status']='interrupted'
                    value['error']='服务曾中断，此记录不会自动续跑或重复调用模型'
                monitor=value.get('monitor')
                if monitor:value['monitor']={**monitor,'historical':True,
                    'stale':bool(monitor.get('stale')) or value['status']=='interrupted'}
                value.update(directory=item,cancel=threading.Event(),restored=True,
                             history=value.get('history',[]),monitor=value.get('monitor'))
                candidates.append(value)
                if len(candidates)>limit:candidates.remove(min(candidates,key=lambda j:j['created']))
            except (OSError,ValueError,TypeError,AttributeError):continue
        return sorted(heapq.nlargest(limit,candidates,key=lambda j:j['created']),key=lambda j:j['created'])
