"""Explicit bounded text continuation; never replay uncertain requests."""
import json,urllib.error,urllib.request
class CompletionFailure(RuntimeError):
    def __init__(self,code):self.code=code
def complete(task,messages):
    limit=task.get('continuation_limit',0)
    if type(limit) is not int or not 0<=limit<=3:raise ValueError('续写最多三次')
    def request(current):
        req=urllib.request.Request(task['_gateway_url'],data=json.dumps({'messages':current,'max_tokens':task.get('max_tokens',4096),'thinking':task.get('thinking',False)},ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+task['_gateway_token'],'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=65) as response:return json.load(response)
        except urllib.error.HTTPError as exc:
            try:body=json.loads(exc.read(8192))
            except (ValueError,OSError):body={}
            code=body.get('error_code') if isinstance(body,dict) else None
            if code not in ('MODEL_UPSTREAM_FAILED','MODEL_TIMEOUT','MODEL_BUDGET_EXHAUSTED'):code='MODEL_BUDGET_EXHAUSTED' if exc.code==429 else 'MODEL_UPSTREAM_FAILED'
            raise CompletionFailure(code) from None
        except (OSError,ValueError):raise CompletionFailure('MODEL_TRANSPORT_FAILED') from None
    parts=[];receipts=[];result=request(messages)
    for number in range(limit+1):
        text=result['content'];overlap=0
        if parts:
            previous=''.join(parts)
            for size in range(min(len(previous),len(text),2000),31,-1):
                if previous.endswith(text[:size]):overlap=size;break
        parts.append(text[overlap:]);receipts.append({k:result[k] for k in ('usage','finish_reason','latency_seconds','thinking_requested') if k in result})
        if result.get('finish_reason')!='length':break
        if number==limit:raise CompletionFailure('MODEL_OUTPUT_INCOMPLETE')
        result=request(messages+[{'role':'assistant','content':''.join(parts)},{'role':'user','content':'上段正文因长度上限中止。只从断点继续，不重写、不重复、不增加前言或代码围栏；保留原始目标与格式。完成全部剩余内容并正确闭合代码/XML。'}])
    result=dict(result);result['content']=''.join(parts);usage={}
    for receipt in receipts:
        for key,value in (receipt.get('usage') or {}).items():
            if type(value) in (int,float):usage[key]=usage.get(key,0)+value
    result.update(usage=usage,provider_calls=len(receipts),continuation_calls=len(receipts)-1,call_receipts=receipts)
    return result
