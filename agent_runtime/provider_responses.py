"""Origin-bound Responses transport for the relay's documented native API.

Only typed assistant output_text is delivered; reasoning items stay private.
No key, request body, provider error body or reasoning is logged.
"""
import hashlib,json,time,urllib.request,urllib.error
from .model_gateway import ModelError,NoRedirect,MAX_BYTES
from .provider_stream import normalized


def error_metadata(value):
    """Return fixed numeric categories only; never expose arbitrary error text."""
    if not isinstance(value,dict):return {'error_kind':0,'error_param':0}
    error=value.get('error',value)
    if not isinstance(error,dict):return {'error_kind':0,'error_param':0}
    code=error.get('code') or error.get('type')
    kinds={'rate_limit_exceeded':1,'server_error':2,'timeout':3,'invalid_request_error':4,'insufficient_quota':5,'context_length_exceeded':6,'model_not_found':7,'unsupported_parameter':8,'invalid_value':9,'invalid_api_key':10}
    params={'input':1,'messages':2,'max_output_tokens':3,'reasoning':4,'reasoning.effort':5,'tools':6,'tool_choice':7,'model':8,'store':9,'stream':10}
    return {'error_kind':kinds.get(code,0) if isinstance(code,str) else 0,'error_param':params.get(error.get('param'),0) if isinstance(error.get('param'),str) else 0}


def encode(messages,*,tools=None):
    instructions=[];items=[]
    for m in messages:
        role=m['role'];content=m.get('content')
        if role in ('system','developer'):
            if not isinstance(content,str):raise ValueError('Invalid instructions')
            instructions.append(content);continue
        if role=='tool':
            items.append({'type':'function_call_output','call_id':m['tool_call_id'],'output':content or ''});continue
        if content:
            if isinstance(content,str):parts=[{'type':'output_text' if role=='assistant' else 'input_text','text':content}]
            elif isinstance(content,list) and role=='user':
                parts=[]
                for part in content:
                    if part['type']=='text':parts.append({'type':'input_text','text':part['text']})
                    elif part['type']=='image_url':parts.append({'type':'input_image','image_url':part['image_url']['url']})
                    else:raise ValueError('Unsupported content')
            else:raise ValueError('Unsupported message content')
            items.append({'type':'message','role':role,'content':parts})
        for call in m.get('tool_calls') or []:
            items.append({'type':'function_call','call_id':call['id'],'name':call['function']['name'],'arguments':call['function']['arguments']})
    if not items:raise ValueError('Missing Responses input')
    result={'input':items,'instructions':'\n\n'.join(instructions) or 'Follow the user request and use available tools when needed.'}
    if tools:
        result['tools']=[{'type':'function',**t['function']} for t in tools]
    return result


def decode(value,model,*,native=False):
    if not isinstance(value,dict) or value.get('status') not in ('completed','incomplete'):raise ModelError('Relay Responses did not complete')
    text=[];calls=[]
    for item in value.get('output',[]):
        if item.get('type')=='message' and item.get('role')=='assistant':
            for part in item.get('content',[]):
                if part.get('type')=='output_text' and isinstance(part.get('text'),str):text.append(part['text'])
        elif item.get('type')=='function_call':
            call_id=item.get('call_id');name=item.get('name');arguments=item.get('arguments')
            if not all(isinstance(v,str) and v for v in (call_id,name,arguments)):raise ModelError('Invalid Responses tool call')
            calls.append({'id':call_id,'type':'function','function':{'name':name,'arguments':arguments}})
    body=''.join(text);usage=value.get('usage') or {}
    if not body.strip() and not(native and calls):raise ModelError('Relay returned no final assistant output')
    normalized_usage={}
    for source,target in (('input_tokens','prompt_tokens'),('output_tokens','completion_tokens'),('total_tokens','total_tokens')):
        number=usage.get(source)
        if type(number) is not int or number<0:raise ModelError('Missing Responses usage')
        normalized_usage[target]=number
    for source,target in (('input_tokens_details','prompt_tokens_details'),('output_tokens_details','completion_tokens_details')):
        if isinstance(usage.get(source),dict):normalized_usage[target]={k:v for k,v in usage[source].items() if type(v) is int and v>=0}
    finish='length' if value['status']=='incomplete' else 'tool_calls' if calls else 'stop'
    if native:
        message={'role':'assistant','content':body or None}
        if calls:message['tool_calls']=calls
        body=json.dumps({'message':message},ensure_ascii=False)
    elif calls:raise ModelError('Unexpected native tool call in text workflow')
    return {'content':body,'usage':normalized_usage,'model':value.get('model',model),'finish_reason':finish}


def complete(client,messages,max_tokens,callback=None,*,native_value=None):
    if type(max_tokens) is not int or not 1<=max_tokens<=4096:raise ModelError('Invalid Responses output budget')
    if native_value is None:normal,thinking=normalized(messages);value=encode(normal)
    else:
        thinking=False;value=encode(native_value['messages'],tools=native_value['tools'])
        if native_value.get('tool_choice') is not None:value['tool_choice']=native_value['tool_choice']
    value.update(model=client.model,max_output_tokens=max_tokens,reasoning={'effort':'high' if thinking else 'minimal'},stream=bool(callback),store=False)
    raw=json.dumps(value,ensure_ascii=False).encode()
    if len(raw)>MAX_BYTES:raise ModelError('Responses request exceeds limit')
    from .model_network import provider_opener
    opener=provider_opener(client,NoRedirect(),default_no_proxy=True)
    request=urllib.request.Request(client.base+'/responses',data=raw,headers={'Authorization':'Bearer '+client.key,'Content-Type':'application/json'})
    from .transport_trace import Trace
    trace=Trace();trace.emit('request_start');events_seen=0;callbacks=0;wire_seen=False;text_seen=False
    callback_opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()) if callback else None
    started=time.monotonic();last=0.;pending=[];enabled=True;response_value=None;size=0;text_bytes=0
    def emit(force=False):
        nonlocal last,enabled,callbacks
        if not callback or not pending or not enabled or not force and time.monotonic()-last<.12:return
        delta=''.join(pending);pending.clear();last=time.monotonic()
        q=urllib.request.Request(callback['url'],data=json.dumps({'delta':delta},ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+callback['token'],'Content-Type':'application/json'})
        try:
            with callback_opener.open(q,timeout=.5) as r:r.read(128)
            callbacks+=1
        except OSError:enabled=False;pending.clear();trace.emit('callback_rejected')
    try:
        with opener.open(request,timeout=client.timeout) as response:
            trace.emit('headers',status=response.status if hasattr(response,'status') else 200,sse='text/event-stream' in getattr(response,'headers',{}).get('Content-Type',''))
            if not callback:
                raw=response.read(MAX_BYTES+1)
                if len(raw)>MAX_BYTES:raise ModelError('Responses output exceeds limit')
                response_value=json.loads(raw)
            else:
                if 'text/event-stream' not in response.headers.get('Content-Type',''):raise ModelError('Responses SSE expected')
                size=0;text_bytes=0;text_hash=hashlib.sha256()
                from .sse import payloads
                def observe_wire(total):
                    nonlocal size,wire_seen
                    size=total
                    if not wire_seen:trace.emit('first_wire');wire_seen=True
                for payload in payloads(response,MAX_BYTES,observe=observe_wire):
                    if payload=='[DONE]':break
                    event=json.loads(payload)
                    if not isinstance(event,dict):raise ValueError('Responses event must be an object')
                    kind=event.get('type');events_seen+=1
                    if kind=='response.output_text.delta':
                        delta=event.get('delta')
                        if not isinstance(delta,str):raise ModelError('Invalid Responses delta')
                        if not text_seen:trace.emit('first_text');text_seen=True
                        text_bytes+=len(delta.encode());text_hash.update(delta.encode())
                        if text_bytes>MAX_BYTES:raise ModelError('Responses text exceeds limit')
                        if enabled:pending.append(delta);emit()
                    elif kind in ('response.completed','response.incomplete'):response_value=event.get('response');trace.emit('terminal',wire_bytes=size,text_bytes=text_bytes,events=events_seen,callbacks=callbacks);break
                    elif kind in ('response.failed','error') or event.get('error'):
                        failed_response=event.get('response') if isinstance(event.get('response'),dict) else {}
                        raw_error=event.get('error') or failed_response.get('error') or event
                        trace.emit('provider_error',events=events_seen,wire_bytes=size,**error_metadata(raw_error))
                        raise ModelError('Responses provider rejected request',code='MODEL_UPSTREAM_FAILED')
        if response_value is None:
            trace.emit('missing_terminal',events=events_seen,wire_bytes=size,text_bytes=text_bytes)
            raise ModelError('Responses stream incomplete; no retry',code='MODEL_UPSTREAM_FAILED')
        result=decode(response_value,client.model,native=native_value is not None)
        if callback and native_value is None and text_bytes and text_hash.hexdigest()!=hashlib.sha256(result['content'].encode()).hexdigest():raise ModelError('Responses final text differs from stream')
        emit(True)
        trace.emit('decoded',events=events_seen,callbacks=callbacks)
        result['latency_seconds']=time.monotonic()-started;return result
    except urllib.error.HTTPError as exc:
        metadata=error_metadata(None)
        try:
            body=exc.read(16385)
            if len(body)<=16384:metadata=error_metadata(json.loads(body))
        except (OSError,ValueError,TypeError):pass
        finally:exc.close()
        trace.emit('http_error',status=exc.code,**metadata)
        raise ModelError('Relay Responses HTTP '+str(exc.code)+'; no retry',code='MODEL_UPSTREAM_FAILED') from None
    except (TimeoutError,urllib.error.URLError) as exc:
        trace.emit('transport_error')
        raise ModelError('Relay transport timeout or connection failure; billing unknown',code='MODEL_TIMEOUT' if isinstance(exc,TimeoutError) or isinstance(getattr(exc,'reason',None),TimeoutError) else 'MODEL_UPSTREAM_FAILED') from None
    except (ValueError,KeyError,TypeError,AttributeError):
        trace.emit('parser_error')
        raise ModelError('Relay Responses schema failed; billing unknown; no retry',code='MODEL_UPSTREAM_FAILED') from None
    except OSError:
        trace.emit('transport_error')
        raise ModelError('Relay Responses failed; billing state unknown; no retry') from None
