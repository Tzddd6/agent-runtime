"""Optional owner-only, expiring loopback egress for OpenAI TLS CONNECT.

This controls transport only. It never receives supplier credentials or modifies
TLS verification, API origin, model, prompt, retries or provider parameters.
"""
import json,os,stat,time
from pathlib import Path
import urllib.request

CONFIG_PATH=Path('/home/tzd/.config/agentrt/openai-network.json')
PROXY_URL='http://127.0.0.1:18889'

def read_proxy(path=CONFIG_PATH,*,now=None):
    try:info=path.lstat()
    except FileNotFoundError:return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)&0o077 or info.st_size>1024):
        raise ValueError('Unsafe OpenAI network configuration')
    parent=path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.geteuid() or stat.S_IMODE(parent.st_mode)&0o077:
        raise ValueError('Unsafe OpenAI network directory')
    value=json.loads(path.read_text())
    current=time.time() if now is None else now
    if (not isinstance(value,dict) or set(value)!={'proxy_url','expires_at','owner_id'}
        or value['proxy_url']!=PROXY_URL or type(value['expires_at']) not in (int,float)
        or not current<value['expires_at']<=current+3600
        or not isinstance(value['owner_id'],str) or not 16<=len(value['owner_id'])<=64):
        raise ValueError('Invalid or expired OpenAI network configuration')
    return PROXY_URL

def provider_opener(client,*handlers,default_no_proxy=False):
    if getattr(client,'provider',None)=='openai' and client.base=='https://api.openai.com/v1' and not client.local_test:
        proxy=read_proxy()
        if proxy:return urllib.request.build_opener(urllib.request.ProxyHandler({'https':proxy}),*handlers)
    if default_no_proxy:return urllib.request.build_opener(urllib.request.ProxyHandler({}),*handlers)
    return urllib.request.build_opener(*handlers)
