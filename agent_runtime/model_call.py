"""Trusted one-call broker child. Its parent owns the absolute deadline."""
import json
import sys
from .model_gateway import DeepSeekClient,ModelError,MAX_BYTES


def main():
    try:
        wire=sys.stdin.buffer.read(MAX_BYTES+4097)
        if len(wire)>MAX_BYTES+4096:raise ModelError('模型代理输入超过限制')
        request=json.loads(wire)
        client=DeepSeekClient(request['key'],model=request['model'],timeout=request['timeout'],
                              base=request['base'],local_test=request.get('local_test',False))
        value=client._complete_direct(request['messages'],request['max_tokens'])
    except ModelError as exc:value={'error':str(exc)}
    except Exception:value={'error':'模型代理子进程请求失败'}
    sys.stdout.write(json.dumps(value,ensure_ascii=False))


if __name__=='__main__':main()
