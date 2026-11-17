"""Native tool-call transport executed only in the trusted credential child.

This carrier preserves official messages and tool schemas; it adds no prompt.
It never executes a returned tool or exposes the provider key in its result.
"""
import json
import math
import time
import urllib.request
from .model_gateway import ModelError, NoRedirect, MAX_BYTES,inference_options

PREFIX='agentrt-native-toolwire-v1:'


def encode(messages, tools=None, *, temperature=0.0, seed=None, tool_choice=None):
    value={'messages':messages,'tools':tools,'temperature':temperature,'seed':seed,'tool_choice':tool_choice}
    validate(value)
    return [{'role':'system','content':PREFIX+json.dumps(value,ensure_ascii=False)}]


def validate(value):
    if not isinstance(value,dict) or set(value)!={'messages','tools','temperature','seed','tool_choice'}:
        raise ValueError('Invalid native benchmark transport')
    messages=value['messages'];tools=value['tools']
    if not isinstance(messages,list) or not 1<=len(messages)<=512:
        raise ValueError('Invalid benchmark messages')
    for message in messages:
        if (not isinstance(message,dict) or message.get('role') not in ('system','user','assistant','tool')
                or set(message)-{'role','content','tool_calls','tool_call_id','name'}
                or message.get('content') is not None and not isinstance(message['content'],str)):
            raise ValueError('Invalid native message')
        if message['role']=='tool' and not isinstance(message.get('tool_call_id'),str):
            raise ValueError('Missing tool call correlation')
    if tools is not None:
        if not isinstance(tools,list) or len(tools)>128:raise ValueError('Too many tools')
        for tool in tools:
            if not isinstance(tool,dict) or tool.get('type')!='function' or not isinstance(tool.get('function'),dict):
                raise ValueError('Invalid tool schema')
    temperature=value['temperature']
    if type(temperature) not in (int,float) or not math.isfinite(temperature) or not 0<=temperature<=2:
        raise ValueError('Invalid temperature')
    if value['seed'] is not None and type(value['seed']) is not int:raise ValueError('Invalid seed')
    if value['tool_choice'] not in (None,'auto','none','required'):raise ValueError('Unsupported tool choice')
    if len(json.dumps(value,ensure_ascii=False).encode())>MAX_BYTES-1024:raise ValueError('Benchmark request too large')


def complete(client, messages, max_tokens):
    if (len(messages)!=1 or messages[0].get('role')!='system'
            or not messages[0].get('content','').startswith(PREFIX)):
        raise ModelError('Invalid benchmark carrier')
    value=json.loads(messages[0]['content'][len(PREFIX):]);validate(value)
    if type(max_tokens) is not int or not 1<=max_tokens<=4096:raise ModelError('Invalid benchmark token limit')
    body={'model':client.model,'messages':value['messages'],
          'temperature':value['temperature'],'stream':False,**inference_options(client,max_tokens)}
    reasoning_openai=getattr(client,'provider','deepseek') in ('openai','aizex') and client.model.startswith(('gpt-5','gpt-6','o1','o3','o4'))
    if reasoning_openai:body.pop('temperature')
    if value['tools']:
        body['tools']=value['tools'];body['tool_choice']=value['tool_choice'] or 'auto'
    if value['seed'] is not None and not reasoning_openai:body['seed']=value['seed']
    request=urllib.request.Request(client.base+'/chat/completions',data=json.dumps(body,ensure_ascii=False).encode(),
        headers={'Authorization':'Bearer '+client.key,'Content-Type':'application/json'})
    start=time.monotonic()
    try:
        from .model_network import provider_opener
        with provider_opener(client,NoRedirect()).open(request,timeout=client.timeout) as response:
            raw=response.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise ValueError('response size')
        response=json.loads(raw);choice=response['choices'][0];message=choice['message']
        if message.get('role')!='assistant':raise ValueError('response role')
        if not isinstance(message.get('content'),str) and not message.get('tool_calls'):raise ValueError('empty response')
        # Preserve the complete native assistant message, including tool IDs.
        return {'content':json.dumps({'message':message},ensure_ascii=False),
                'usage':response.get('usage',{}),'model':response.get('model',client.model),
                'finish_reason':choice.get('finish_reason'),'latency_seconds':time.monotonic()-start}
    except Exception:
        raise ModelError('Native benchmark provider call failed; remote billing state may be unknown') from None
