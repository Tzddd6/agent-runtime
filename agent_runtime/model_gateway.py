"""控制进程内的 DeepSeek 网关。Worker 只持有临时本地会话 token。"""
import json
import os
import secrets
import threading
import time
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MAX_BYTES = 1024 * 1024


class ModelError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不将 Authorization 跟随重定向交给其他来源。
        return None


class DeepSeekClient:
    def __init__(self, key, model='deepseek-flash', timeout=30, base='https://api.deepseek.com', *, local_test=False):
        if not key:
            raise ModelError('请在控制进程环境设置 DEEPSEEK_API_KEY')
        if base != 'https://api.deepseek.com':
            from urllib.parse import urlparse
            url = urlparse(base)
            if not local_test or url.scheme != 'http' or url.hostname != '127.0.0.1':
                raise ModelError('仅允许官方地址；离线测试只能使用显式启用的本地 mock')
        self.key, self.model, self.timeout, self.base = key, model, timeout, base
        self.local_test = local_test
        self.cancel_event = None

    def for_request(self,cancel_event):
        import copy
        client=copy.copy(self);client.cancel_event=cancel_event
        return client

    def complete(self, messages, max_tokens=256):
        # Isolate blocking DNS/TLS/socket reads behind one absolute local deadline.
        # The child is a trusted model broker, never an Agent/tool Worker.
        wire=json.dumps({'key':self.key,'model':self.model,'timeout':self.timeout,
                         'base':self.base,'local_test':self.local_test,
                         'messages':messages,'max_tokens':max_tokens},ensure_ascii=False).encode()
        if len(wire)>MAX_BYTES+4096:
            raise ModelError('模型请求超过 1 MiB')
        environment=dict(os.environ)
        environment.pop('DEEPSEEK_API_KEY',None)
        environment.pop('AGENTRT_PROXY_TOKEN',None)
        deadline=time.monotonic()+self.timeout
        proc=subprocess.Popen([sys.executable,'-X','utf8','-m','agent_runtime.model_call'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=environment)
        first=True
        try:
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise ModelError('本地模型调用已取消；远端计算与计费状态未知')
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    raise ModelError('模型调用超过绝对总期限；远端计算与计费状态未知')
                try:
                    raw,_=proc.communicate(input=wire if first else None,timeout=min(.1,remaining))
                    break
                except subprocess.TimeoutExpired:
                    first=False
            if proc.returncode!=0 or len(raw)>2*MAX_BYTES:
                raise ModelError('模型代理子进程失败或响应超过限制')
            value=json.loads(raw)
            if 'error' in value:raise ModelError(value['error'])
            return value
        except (OSError,ValueError):
            raise ModelError('模型代理通信失败') from None
        finally:
            if proc.poll() is None:proc.kill()
            proc.communicate(timeout=3)

    def _complete_direct(self, messages, max_tokens=256):
        if not isinstance(messages, list) or not messages:
            raise ModelError('messages 必须为非空数组')
        for msg in messages:
            if (not isinstance(msg, dict) or msg.get('role') not in ('system', 'user', 'assistant')
                    or not isinstance(msg.get('content'), str)):
                raise ModelError('消息必须包含合法 role 和字符串 content')
        if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
            raise ModelError('max_tokens 必须为 1—4096 的整数')
        data = json.dumps({'model': self.model, 'messages': messages,
                           'max_tokens': max_tokens, 'stream': False,
                           'thinking':{'type':'disabled'}}).encode()
        if len(data) > MAX_BYTES:
            raise ModelError('模型请求超过 1 MiB')
        req = urllib.request.Request(self.base+'/chat/completions', data=data,
                    headers={'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json'})
        started = time.monotonic()
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=self.timeout) as response:
                raw = response.read(MAX_BYTES+1)
            if len(raw) > MAX_BYTES:
                raise ModelError('模型响应超过 1 MiB')
            value = json.loads(raw)
            text = value['choices'][0]['message']['content']
            if not isinstance(text, str):
                raise ValueError('invalid content')
            if not text.strip():
                raise ModelError('模型未返回正文，可能已耗尽输出预算；不能视为任务成功')
            return {'content': text, 'model': value.get('model', self.model),
                    'usage': value.get('usage', {}), 'finish_reason': value['choices'][0].get('finish_reason'),
                    'latency_seconds': time.monotonic()-started}
        except urllib.error.HTTPError as exc:
            raise ModelError(f'模型服务 HTTP {exc.code}；未自动重试') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ModelError('模型连接失败或超时；远端是否继续计算及计费未知') from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError('模型响应格式不符合约定') from None


class ModelGateway:
    def __init__(self, client, log_path, concurrency=2):
        self.client = client
        self.token = secrets.token_urlsafe(32)
        self.slots = threading.BoundedSemaphore(concurrency)
        self.log_path = log_path
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.capabilities={}
        self.connections, self.connection_lock = set(), threading.Lock()
        if hasattr(client,'cancel_event'):client.cancel_event=self.cancel_event
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(5)
                with owner.connection_lock:owner.connections.add(self.request)
                super().setup()

            def finish(self):
                try:super().finish()
                finally:
                    with owner.connection_lock:owner.connections.discard(self.request)

            def log_message(self, *args):
                pass

            def do_POST(self):
                result, code = {}, 200
                entered = False
                started = time.monotonic()
                try:
                    authorization=self.headers.get('Authorization','').removeprefix('Bearer ')
                    with owner.lock:capability=owner.capabilities.get(authorization)
                    legacy=secrets.compare_digest(authorization,owner.token)
                    if self.path != '/complete' or (not legacy and capability is None):
                        code = 403
                        raise ModelError('未授权的本地模型请求')
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= MAX_BYTES:
                        raise ModelError('本地请求长度无效')
                    self.connection.settimeout(5)
                    value = json.loads(self.rfile.read(length))
                    entered = owner.slots.acquire(timeout=owner.client.timeout)
                    if not entered:
                        code = 429
                        raise ModelError('本地模型槽位等待超时')
                    client=owner.client
                    if capability is not None:
                        if capability.is_set() or owner.cancel_event.is_set():raise ModelError('Agent 模型请求已撤销')
                        # Each real provider/broker client gets an independent cancel
                        # event; cancelling one Agent cannot cancel another Agent.
                        if hasattr(client,'for_request'):
                            class Cancel:
                                def is_set(self):return capability.is_set() or owner.cancel_event.is_set()
                            client=client.for_request(Cancel())
                    result = client.complete(value.get('messages'), value.get('max_tokens', 256))
                except (ModelError, ValueError, TypeError, AttributeError, OSError) as exc:
                    if code == 200:
                        code = 502
                    result = {'error': str(exc) if isinstance(exc, ModelError) else '本地模型请求格式错误'}
                finally:
                    if entered:
                        owner.slots.release()
                # 不记录密钥、临时 token、提示词或模型正文。
                with owner.lock:
                    with owner.log_path.open('a', encoding='utf-8') as log:
                        log.write(json.dumps({'time': time.time(), 'status': code,
                            'model': owner.client.model, 'usage': result.get('usage', {}),
                            'total_seconds': time.monotonic()-started}, ensure_ascii=False)+'\n')
                payload = json.dumps(result, ensure_ascii=False).encode()
                try:
                    self.send_response(code)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        # 关闭时等待本地调用线程收敛，避免取消后立刻叠加新工作流的调用槽。
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/complete'

    @classmethod
    def from_env(cls, log_path):
        if os.environ.get('AGENTRT_MODEL_BROKER_URL'):
            from .model_broker import BrokerClient
            return cls(BrokerClient.from_env(),log_path)
        return cls(DeepSeekClient(os.environ.get('DEEPSEEK_API_KEY'),
            model=os.environ.get('DEEPSEEK_MODEL', 'deepseek-flash')), log_path)

    def issue(self,agent):
        token=secrets.token_urlsafe(32)
        with self.lock:self.capabilities[token]=threading.Event()
        return token

    def revoke(self,token):
        with self.lock:
            event=self.capabilities.pop(token,None)
            if event:event.set()

    def close(self):
        self.cancel_event.set()
        self.server.shutdown()
        with self.connection_lock:connections=list(self.connections)
        for connection in connections:
            try:connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        self.server.server_close()
        self.thread.join(timeout=2)
