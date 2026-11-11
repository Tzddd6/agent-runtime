"""Trusted one-call broker child. Its parent owns the absolute deadline."""
import json
import sys
import os
import stat
from pathlib import Path
from .model_gateway import DeepSeekClient,ModelError,MAX_BYTES


HANDOFF_PATH=Path('/home/tzd/.config/agentrt/broker-handoff.json')


def handoff_existing_key(key,*,path=HANDOFF_PATH,client_factory=None):
    """Explicit same-host memory handoff. Neither marker nor proxy file has a supplier key.

    Enabled only by the administrator's private marker, owned by this Unix user.
    Tests may supply a temporary path/client; the CLI always uses the fixed path.
    Failure is closed: no supplier call is made, and the marker stays for recovery.
    """
    try:info=path.lstat()
    except FileNotFoundError:return False
    if os.name!='posix':raise ModelError('Memory handoff requires the protected Linux owner')
    def private_file(target):
        value=target.lstat()
        if (not stat.S_ISREG(value.st_mode) or value.st_uid!=os.geteuid()
                or stat.S_IMODE(value.st_mode)&0o077):
            raise ModelError('Unsafe memory-handoff configuration permissions')
        return value
    private_file(path)
    parent=path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.geteuid()
            or stat.S_IMODE(parent.st_mode)&0o077):
        raise ModelError('Unsafe memory-handoff directory permissions')
    if info.st_size>2048:raise ModelError('Memory-handoff marker exceeds limit')
    marker=json.loads(path.read_text())
    from .model_broker import BrokerClient,BROKER_URL
    if (not isinstance(marker,dict) or set(marker)!={'url','nonce'} or marker['url']!=BROKER_URL
            or not isinstance(marker['nonce'],str) or not 16<=len(marker['nonce'])<=128):
        raise ModelError('Invalid memory-handoff marker')
    proxy_path=path.parent/'console.env'
    if private_file(proxy_path).st_size>4096:raise ModelError('Proxy configuration exceeds limit')
    values={}
    for line in proxy_path.read_text().splitlines():
        if line.startswith('AGENTRT_PROXY_TOKEN='):values['token']=line.split('=',1)[1]
    token=values.get('token','')
    if len(token)<16:raise ModelError('Proxy control token unavailable for memory handoff')
    client=(client_factory or (lambda token:BrokerClient(token)))(token)
    if not client.configure(key).get('configured'):raise ModelError('Memory-handoff was not confirmed')
    path.unlink()
    return True


def main():
    try:
        wire=sys.stdin.buffer.read(MAX_BYTES+4097)
        if len(wire)>MAX_BYTES+4096:raise ModelError('模型代理输入超过限制')
        request=json.loads(wire)
        if request.get('base')=='https://api.deepseek.com' and not request.get('local_test',False):
            handoff_existing_key(request['key'])
        client=DeepSeekClient(request['key'],model=request['model'],timeout=request['timeout'],
                              base=request['base'],local_test=request.get('local_test',False))
        value=client._complete_direct(request['messages'],request['max_tokens'])
    except ModelError as exc:value={'error':str(exc)}
    except Exception:value={'error':'模型代理子进程请求失败'}
    sys.stdout.write(json.dumps(value,ensure_ascii=False))


if __name__=='__main__':main()
