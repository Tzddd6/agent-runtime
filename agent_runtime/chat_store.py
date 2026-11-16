"""Bounded private conversation records. Durable intent prevents paid POST replay."""
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

MODES=('auto','llm','team','review','document','engineering')
LIMIT=8*1024**2

def identity(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{32}',value):
        raise ValueError('会话和请求 ID 必须为 32 位十六进制字符')
    return value


class ChatStore:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock=threading.RLock()
        self.listing_cache={}
        # Recovery is observation only. Never resubmit an uncertain model call.
        for path in self.root.glob('*.json'):
            try:
                chat=self.read(path.stem);changed=False
                for turn in chat['turns']:
                    if turn['status']=='running':
                        turn.update(status='interrupted',answer='服务曾中断，请在控制台核对原运行。此请求不会自动重试。');changed=True
                if changed:self.save(chat)
            except (ValueError,OSError,KeyError,TypeError):continue

    def read(self,key):
        path=self.root/(identity(key)+'.json')
        with self.lock:
            if path.is_symlink():raise ValueError('会话文件不允许符号链接')
            with path.open('rb') as f:raw=f.read(LIMIT+1)
            if len(raw)>LIMIT:raise ValueError('会话文件超过上限')
            value=json.loads(raw)
            if (not isinstance(value,dict) or value.get('id')!=key or
                not isinstance(value.get('turns'),list) or len(value['turns'])>100 or
                not isinstance(value.get('title'),str) or not isinstance(value.get('updated'),(int,float))):
                raise ValueError('会话记录无效')
            for turn in value['turns']:
                if (not isinstance(turn,dict) or not isinstance(turn.get('prompt'),str) or
                    not isinstance(turn.get('answer'),str) or turn.get('mode') not in MODES or
                    turn.get('status') not in ('running','succeeded','failed','cancelled','interrupted','recovery_required')):
                    raise ValueError('会话消息无效')
                identity(turn.get('request_id'))
            stat=path.stat();self.listing_cache[key]=(stat.st_mtime_ns,stat.st_size,{k:value[k] for k in ('id','title','updated')})
            return value

    def save(self,chat):
        target=self.root/(identity(chat['id'])+'.json')
        raw=json.dumps(chat,ensure_ascii=False,allow_nan=False).encode()
        if len(raw)>LIMIT:raise ValueError('会话记录已满，请新建对话')
        temporary=self.root/('.chat-'+secrets.token_hex(8))
        with self.lock:
            if target.is_symlink():raise ValueError('会话文件不允许符号链接')
            try:
                fd=os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
                os.replace(temporary,target)
                stat=target.stat();self.listing_cache[chat['id']]=(stat.st_mtime_ns,stat.st_size,{k:chat[k] for k in ('id','title','updated')})
            finally:temporary.unlink(missing_ok=True)

    def listing(self):
        values=[]
        with self.lock:
            for p in self.root.glob('*.json'):
                try:
                    if p.is_symlink():continue
                    stat=p.stat();cached=self.listing_cache.get(p.stem)
                    if cached and cached[:2]==(stat.st_mtime_ns,stat.st_size):values.append(dict(cached[2]))
                    else:
                        c=self.read(p.stem);values.append({k:c[k] for k in ('id','title','updated')})
                except (ValueError,OSError,KeyError,TypeError):continue
        return sorted(values,key=lambda c:c['updated'],reverse=True)

    def intent(self,key,request,prompt,mode,project,budget,*,response_tokens=4096,deep_thinking=False,attachments=None,use_knowledge=False,context_mode='balanced'):
        identity(key);identity(request)
        with self.lock:
            try:chat=self.read(key)
            except FileNotFoundError:
                if len(list(self.root.glob('*.json')))>=100:raise ValueError('已达到 100 个会话上限，请管理员归档记录')
                chat={'id':key,'title':prompt[:32] or '项目修复','updated':time.time(),'turns':[]}
            signature={'prompt':prompt,'mode':mode,'project':project,'model_budget':budget,'response_tokens':response_tokens,'deep_thinking':deep_thinking,'attachments':attachments or [],'use_knowledge':use_knowledge,'context_mode':context_mode}
            for turn in chat['turns']:
                if turn['request_id']==request:
                    if any(turn.get(k,'full' if k=='context_mode' else 4096 if k=='response_tokens' else False if k in ('deep_thinking','use_knowledge') else [] if k=='attachments' else None)!=v for k,v in signature.items()):raise ValueError('同一请求 ID 不允许更改内容')
                    if not turn.get('job_id'):raise ValueError('原请求的执行状态尚未确认，请打开控制台核对，不要重复提交')
                    return chat,turn,True
            if any(t['status']=='running' for t in chat['turns']):raise ValueError('请先等待当前回答或停止运行')
            if len(chat['turns'])>=100:raise ValueError('当前会话已达 100 轮，请归档后新建对话')
            turn={**signature,'request_id':request,'job_id':None,'created':time.time(),'status':'running','answer':''}
            chat['turns'].append(turn);chat['updated']=time.time();self.save(chat)
            return chat,turn,False

    def update(self,key,request,**changes):
        with self.lock:
            chat=self.read(key);turn=next(t for t in chat['turns'] if t['request_id']==request)
            turn.update(changes);chat['updated']=time.time();self.save(chat)


def history_messages(chat):
    pairs=[];used=0
    for turn in reversed(chat['turns'][:-1]):
        if turn['status']!='succeeded':continue
        pair=[{'role':'user','content':turn['prompt']},{'role':'assistant','content':turn['answer']}]
        size=len(json.dumps(pair,ensure_ascii=False).encode())
        if used+size>98304 or len(pairs)>=16:break
        pairs.append(pair);used+=size
    return [message for pair in reversed(pairs) for message in pair]


def answer_for(job):
    results=[(e['task_id'],e.get('result',{}).get('result')) for e in job.get('events',[]) if e['state']=='succeeded']
    mode=job['preset']
    if mode=='auto':
        answer=next((v for k,v in reversed(results) if k=='auto_answer'),None)
        if answer is None:
            gate=next((v for k,v in reversed(results) if (k=='route_gate' or k.startswith(('auto_cycle_','repair_check_'))) and isinstance(v,dict) and 'answer' in v),{})
            answer=gate.get('answer')
    elif mode=='llm':answer=next((v for k,v in reversed(results) if k=='assistant'),None)
    elif mode=='team':answer=next((v for k,v in reversed(results) if k=='review'),None)
    elif mode=='review':
        gate=next((v for k,v in reversed(results) if k.startswith('gate-') and isinstance(v,dict)),{})
        answer=gate.get('answer')
        if answer and not gate.get('accepted'):answer+='\n\n评审已达到轮数上限，仍需人工检查。'
    elif mode=='document':
        answer=next((v for k,v in reversed(results) if k=='review'),None)
        if isinstance(answer,dict):
            report=answer.get('report',{});analysis=report.get('analyze',{});index=report.get('index',{})
            fragments=[str(item[0])[:64] for item in index.get('top_words',[])[:6] if isinstance(item,list) and item]
            answer=f"文档分析已完成，共 {analysis.get('characters','—')} 个字符、{analysis.get('lines','—')} 行。"
            if fragments:answer+='\n\n索引中出现的文字片段包括：\n'+'\n'.join('• '+item for item in fragments)
            answer+='\n\n如需内容总结或改写，可切换到直接对话继续提问。完整统计记录保存在运行控制台。'
    else:
        engineering=job.get('engineering',{});verdict=engineering.get('verdict') or {}
        answer=('修复已通过测试与评审。' if verdict.get('accepted') else '本次修复未获接受，请查看测试与评审结果。')
        answer+='\n\n可在独立控制台查看补丁源码、真实测试和评审记录。'
    if job['status']!='succeeded':
        from .failures import job_failure
        problem=job.get('error') or ('运行已停止。' if job['status']=='cancelled' else job_failure(job)['message'])
        return problem+('\n\n已完成部分：\n'+str(answer) if answer else '')
    return str(answer) if answer is not None else '运行已完成。详细结果见控制台。'
