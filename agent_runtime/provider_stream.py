"""Trusted SSE transport. Emits content only to a one-call loopback capability."""
import base64,json,time,urllib.error,urllib.parse,urllib.request
from .model_gateway import ModelError,NoRedirect,MAX_BYTES,inference_options
THINKING_MARKER='agentrt-internal-options-v1:thinking=high'
PREFIX='agentrt-internal-stream-v1:'

def extract(messages):
    if not messages or messages[0].get('role')!='system' or not messages[0].get('content','').startswith(PREFIX):return messages,None
    value=json.loads(messages[0]['content'][len(PREFIX):]);url=urllib.parse.urlsplit(value.get('url',''))
    if (set(value)!={'url','token'} or url.scheme!='http' or url.hostname!='127.0.0.1' or not url.port or url.path!='/progress'
        or url.username or url.password or url.query or url.fragment or not isinstance(value['token'],str) or not 32<=len(value['token'])<=128):raise ModelError('Invalid streaming capability')
    return messages[1:],value

def normalized(messages):
    thinking=bool(messages and messages[0]=={'role':'system','content':THINKING_MARKER})
    if thinking:messages=messages[1:]
    normal=[];images=[]
    for msg in messages:
        if not isinstance(msg,dict) or msg.get('role') not in ('system','user','assistant') or not isinstance(msg.get('content'),str):raise ModelError('Invalid streaming messages')
        if msg['role']=='system' and msg['content'].startswith('agentrt-internal-images-v1:'):
            value=json.loads(msg['content'].split(':',1)[1])
            if not isinstance(value,list):raise ModelError('Invalid streaming images')
            images.extend(value)
        else:normal.append(msg)
    if len(images)>2 or not normal:raise ModelError('Invalid streaming input')
    if images:
        parts=[{'type':'text','text':'用户上传的图片，请结合当前需求分析。'}]
        for image in images:
            if not isinstance(image,dict) or set(image)!={'mime','data'} or image['mime']!='image/jpeg' or not isinstance(image['data'],str) or len(image['data'])>350000:raise ModelError('Invalid streaming image')
            if not base64.b64decode(image['data'],validate=True).startswith(b'\xff\xd8\xff'):raise ModelError('Invalid streaming JPEG')
            parts.append({'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+image['data']}})
        normal.append({'role':'user','content':parts})
    return normal,thinking

def complete(client,messages,max_tokens,callback):
    if getattr(client,'provider',None)=='aizex':
        from .provider_responses import complete as response_complete
        return response_complete(client,messages,max_tokens,callback)
    if type(max_tokens) is not int or not 1<=max_tokens<=4096:raise ModelError('Invalid output budget')
    messages,thinking=normalized(messages)
    value={'model':client.model,'messages':messages,'stream':True,'stream_options':{'include_usage':True},**inference_options(client,max_tokens,thinking)}
    data=json.dumps(value,ensure_ascii=False).encode()
    if len(data)>MAX_BYTES:raise ModelError('Streaming request exceeds limit')
    from .model_network import provider_opener
    opener=provider_opener(client,NoRedirect(),default_no_proxy=True)
    req=urllib.request.Request(client.base+'/chat/completions',data=data,headers={'Authorization':'Bearer '+client.key,'Content-Type':'application/json'})
    pending=[];pending_chars=0;output_bytes=0;last=0;enabled=True;start=time.monotonic();parts=[];usage={};finish=None;model=client.model;wire=0;done=False
    def emit(force=False):
        nonlocal last,enabled,pending_chars
        if not pending or not enabled or not force and time.monotonic()-last<.12 and pending_chars<600:return
        delta=''.join(pending);pending.clear();pending_chars=0;last=time.monotonic()
        update=urllib.request.Request(callback['url'],data=json.dumps({'delta':delta},ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+callback['token'],'Content-Type':'application/json'})
        try:
            with opener.open(update,timeout=.5) as response:response.read(128)
        except OSError:enabled=False # keep the one paid inference, never replay it.
    try:
        with opener.open(req,timeout=client.timeout) as response:
            if 'text/event-stream' not in response.headers.get('Content-Type',''):raise ModelError('Expected provider SSE')
            while True:
                line=response.readline(MAX_BYTES+1);wire+=len(line)
                if not line:break
                if len(line)>MAX_BYTES or wire>8*MAX_BYTES:raise ModelError('Streaming response exceeds limit')
                if not line.startswith(b'data:'):continue
                payload=line[5:].strip()
                if payload==b'[DONE]':done=True;break
                chunk=json.loads(payload);model=chunk.get('model',model)
                if isinstance(chunk.get('usage'),dict):usage=chunk['usage']
                for choice in chunk.get('choices',[]):
                    delta=choice.get('delta',{}).get('content')
                    if delta is not None:
                        if not isinstance(delta,str):raise ModelError('Invalid text delta')
                        parts.append(delta);pending.append(delta);pending_chars+=len(delta);output_bytes+=len(delta.encode())
                        if output_bytes>MAX_BYTES:raise ModelError('Text exceeds limit')
                        emit()
                    if choice.get('finish_reason') is not None:finish=choice['finish_reason']
        emit(True)
        if not done or not finish or not ''.join(parts).strip():raise ModelError('Streaming output incomplete; no retry')
        return {'content':''.join(parts),'model':model,'usage':usage,'finish_reason':finish,'latency_seconds':time.monotonic()-start}
    except urllib.error.HTTPError as exc:raise ModelError('Streaming provider HTTP '+str(exc.code)+'; no retry') from None
    except (OSError,ValueError,KeyError,TypeError):raise ModelError('Streaming transport failed; remote billing state unknown; no retry') from None
