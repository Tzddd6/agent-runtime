"""控制进程内的 DeepSeek 网关。Worker 只持有临时本地会话 token。"""
import json
import math
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
from .model_budget import ModelBudgetExceeded


MAX_BYTES = 1024 * 1024
THINKING_MARKER='agentrt-internal-options-v1:thinking=high'


def numeric_timing(value):
    return value if type(value) in (int,float) and math.isfinite(value) and value>=0 else None


def public_timings(value):
    if not isinstance(value,dict):return {}
    return {key:numeric_timing(value[key]) for key in
            ('client_seconds','broker_seconds','completion_detection_seconds','status_reads') if key in value}


class ModelError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不将 Authorization 跟随重定向交给其他来源。
        return None


def inference_options(client,max_tokens,thinking=False):
    """Provider-specific output bounds; never send another vendor's options."""
    if getattr(client,'provider','deepseek') in ('openai','aizex'):
        result={'max_completion_tokens':max_tokens,'store':False}
        if getattr(client,'provider',None)=='aizex' and client.model.startswith(('gpt-5','gpt-6')) and not thinking:result['reasoning_effort']='minimal'
        if thinking:
            if not client.model.startswith(('gpt-5','gpt-6','o1','o3','o4')):
                raise ModelError('所选 OpenAI 模型未启用推理参数，请关闭深度思考或更换模型')
            result['reasoning_effort']='high'
        return result
    result={'max_tokens':max_tokens,'thinking':{'type':'enabled' if thinking else 'disabled'}}
    if thinking:result['reasoning_effort']='high'
    return result


class DeepSeekClient:
    supports_stream_callbacks=True
    provider='deepseek'
    official_base='https://api.deepseek.com'
    def __init__(self, key, model='deepseek-flash', timeout=30, base='https://api.deepseek.com', *, local_test=False):
        if not key:
            raise ModelError('请在控制进程环境设置 DEEPSEEK_API_KEY')
        if base != self.official_base:
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
        wire=json.dumps({'key':self.key,'model':self.model,'timeout':self.timeout,'provider':self.provider,
                         'base':self.base,'local_test':self.local_test,
                         'messages':messages,'max_tokens':max_tokens},ensure_ascii=False).encode()
        if len(wire)>MAX_BYTES+4096:
            raise ModelError('模型请求超过 1 MiB')
        environment=dict(os.environ)
        environment.pop('DEEPSEEK_API_KEY',None)
        environment.pop('OPENAI_API_KEY',None)
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
        thinking=messages[0]=={'role':'system','content':'agentrt-internal-options-v1:thinking=high'}
        if thinking:messages=messages[1:]
        if not messages:raise ModelError('messages 必须包含实际请求')
        options=inference_options(self,max_tokens,thinking)
        import base64
        normal=[];images=[]
        for message in messages:
            if message['role']=='system' and message['content'].startswith('agentrt-internal-images-v1:'):
                value=json.loads(message['content'].split(':',1)[1])
                if not isinstance(value,list):raise ModelError('图片参数无效')
                images.extend(value)
            else:normal.append(message)
        if len(images)>2:raise ModelError('每次最多两张图片')
        if images:
            parts=[{'type':'text','text':'用户上传的图片，请结合当前需求分析。'}]
            for image in images:
                if (not isinstance(image,dict) or set(image)!={'mime','data'} or image['mime']!='image/jpeg'
                    or not isinstance(image['data'],str) or len(image['data'])>350000):raise ModelError('图片参数无效')
                raw_image=base64.b64decode(image['data'],validate=True)
                if not raw_image.startswith(b'\xff\xd8\xff'):raise ModelError('仅允许标准化JPEG图片')
                parts.append({'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+image['data']}})
            normal.append({'role':'user','content':parts})
        messages=normal
        data = json.dumps({'model': self.model, 'messages': messages,
                           'stream': False,
                           **options},ensure_ascii=False).encode()
        if len(data) > MAX_BYTES:
            raise ModelError('模型请求超过 1 MiB')
        req = urllib.request.Request(self.base+'/chat/completions', data=data,
                    headers={'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json'})
        started = time.monotonic()
        try:
            from .model_network import provider_opener
            with provider_opener(self,NoRedirect()).open(req, timeout=self.timeout) as response:
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


class OpenAIClient(DeepSeekClient):
    """Official OpenAI Chat Completions transport, including SSE and images."""
    provider='openai'
    official_base='https://api.openai.com/v1'

    def __init__(self,key,model='gpt-4.1-mini',timeout=30,base='https://api.openai.com/v1',*,local_test=False):
        super().__init__(key,model,timeout,base,local_test=local_test)


class AizexClient(OpenAIClient):
    """Explicit third-party origin, separate from official OpenAI credentials."""
    provider='aizex'
    official_base='https://ca.memofun.net/v1'

    def __init__(self,key,model='gpt-6-luna',timeout=30,base='https://ca.memofun.net/v1',*,local_test=False):
        super().__init__(key,model,timeout,base,local_test=local_test)


class ModelGateway:
    def __init__(self, client, log_path, concurrency=2):
        self.memory_repository=None
        self.memory_budget_bytes=8192
        self.client = client
        self.token = secrets.token_urlsafe(32)
        self.slots = threading.BoundedSemaphore(concurrency)
        self.log_path = log_path
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.capabilities={}
        self.budgets=None
        self.output_observer=None
        self.stream_tokens={}
        self.connections, self.connection_lock = set(), threading.Lock()
        if hasattr(client,'for_request'):self.client=client.for_request(self.cancel_event)
        elif hasattr(client,'cancel_event'):client.cancel_event=self.cancel_event
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
                if self.path=='/progress':
                    try:
                        token=self.headers.get('Authorization','').removeprefix('Bearer ')
                        length=int(self.headers.get('Content-Length','0'))
                        if not 0<length<=32768:raise ValueError('Invalid delta size')
                        value=json.loads(self.rfile.read(length));delta=value.get('delta')
                        if set(value)!={'delta'} or not isinstance(delta,str) or len(delta)>16000:raise ValueError('Invalid delta')
                        with owner.lock:
                            stream=owner.stream_tokens.get(token)
                            if stream is None or stream['cap']['cancel'].is_set() or owner.cancel_event.is_set():raise ValueError('Revoked delta capability')
                            cap=stream['cap'];cap['preview']=(cap['preview']+delta)[:32000];stream['deltas']+=1
                            if stream['first'] is None:stream['first']=time.monotonic()-stream['started']
                            event={'task_id':cap['stream_key'],'text':cap['preview'],'updated':time.time(),'source':'provider_content_delta'}
                        if owner.output_observer:
                            try:owner.output_observer(event)
                            except Exception:pass # a UI subscriber cannot replay/fail the paid call.
                        self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'{}')
                    except (ValueError,TypeError,OSError):
                        try:self.send_response(403);self.send_header('Content-Length','0');self.end_headers()
                        except OSError:pass
                    return
                result, code = {}, 200
                entered = False
                started = time.monotonic()
                queue_seconds = None
                client_seconds = None
                stream_token=None;stream=None
                memory_diagnostics=None
                try:
                    authorization=self.headers.get('Authorization','').removeprefix('Bearer ')
                    with owner.lock:capability=owner.capabilities.get(authorization)
                    legacy=secrets.compare_digest(authorization,owner.token)
                    if self.path != '/complete' or (not legacy and capability is None) or (legacy and owner.budgets is not None):
                        code = 403
                        raise ModelError('未授权的本地模型请求')
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= MAX_BYTES:
                        raise ModelError('本地请求长度无效')
                    self.connection.settimeout(5)
                    value = json.loads(self.rfile.read(length))
                    messages,max_tokens=value.get('messages'),value.get('max_tokens',256)
                    thinking=value.get('thinking',False)
                    if (not isinstance(messages,list) or not messages or
                        any(not isinstance(m,dict) or m.get('role') not in ('system','user','assistant')
                            or not isinstance(m.get('content'),str) for m in messages)
                        or type(max_tokens) is not int or not 1<=max_tokens<=4096 or type(thinking) is not bool):
                        code=400;raise ModelError('模型请求格式无效')
                    messages=[m for m in messages if m!={'role':'system','content':THINKING_MARKER}]
                    from .provider_stream import PREFIX
                    messages=[m for m in messages if not (m.get('role')=='system' and m['content'].startswith(PREFIX))]
                    if not messages:code=400;raise ModelError('消息缺少实际请求')
                    if owner.memory_repository is not None:
                        if capability is None:
                            code=403;raise ModelError('记忆访问需要 Agent 临时凭证')
                        try:
                            messages,memory_diagnostics=owner.memory_repository.inject(
                                capability['agent'],messages,owner.memory_budget_bytes)
                        except Exception:
                            code=503;raise ModelError('上下文存储不可用，已阻止模型调用') from None
                    if thinking:messages=[{'role':'system','content':THINKING_MARKER}]+messages
                    deadline=time.monotonic()+owner.client.timeout
                    while not entered:
                        if owner.cancel_event.is_set() or (capability is not None and capability['cancel'].is_set()):
                            raise ModelError('Agent 模型请求已撤销')
                        remaining=deadline-time.monotonic()
                        if remaining<=0:
                            code=429;raise ModelError('本地模型槽位等待超时')
                        entered=owner.slots.acquire(timeout=min(.05,remaining))
                    queue_seconds=time.monotonic()-started
                    client=owner.client
                    if capability is not None:
                        # Serialize revocation with admission. A queued revoked
                        # request neither starts nor consumes the model budget.
                        with owner.lock:
                            if capability['cancel'].is_set() or owner.cancel_event.is_set():raise ModelError('Agent 模型请求已撤销')
                            if owner.budgets is not None:
                                try:owner.budgets.admit(capability['agent'],max_tokens)
                                except ModelBudgetExceeded as exc:
                                    code=429;raise ModelError(str(exc)) from None
                        # Each real provider/broker client gets an independent cancel
                        # event; cancelling one Agent cannot cancel another Agent.
                        if hasattr(client,'for_request'):
                            class Cancel:
                                def is_set(self):return capability['cancel'].is_set() or owner.cancel_event.is_set()
                            client=client.for_request(Cancel())
                    call_started=time.monotonic()
                    if capability is not None and capability.get('stream_key') and owner.output_observer and getattr(client,'supports_stream_callbacks',False):
                        stream_token=secrets.token_urlsafe(32)
                        stream={'cap':capability,'deltas':0,'first':None,'started':call_started}
                        with owner.lock:owner.stream_tokens[stream_token]=stream
                        marker={'url':owner.url.removesuffix('/complete')+'/progress','token':stream_token}
                        messages=[{'role':'system','content':PREFIX+json.dumps(marker)}]+messages
                    result = client.complete(messages,max_tokens)
                    if stream is not None:result.update(stream_deltas=stream['deltas'],first_delta_seconds=stream['first'])
                    result['thinking_requested']=thinking
                    client_seconds=time.monotonic()-call_started
                except (ModelError, ValueError, TypeError, AttributeError, OSError) as exc:
                    if code == 200:
                        code = 502
                    from .failures import failure,model_code
                    public=failure(model_code(exc))
                    result = {'error':public['message'],'error_code':public['code']}
                finally:
                    if stream_token:
                        with owner.lock:owner.stream_tokens.pop(stream_token,None)
                    if entered:
                        owner.slots.release()
                # 不记录密钥、临时 token、提示词或模型正文。
                with owner.lock:
                    with owner.log_path.open('a', encoding='utf-8') as log:
                        log.write(json.dumps({'time': time.time(), 'status': code,'error_code':result.get('error_code'),
                            'model': owner.client.model, 'usage': result.get('usage', {}),
                            'total_seconds': time.monotonic()-started,
                            'gateway_queue_seconds':queue_seconds,'client_seconds':client_seconds,
                            'provider_http_seconds':numeric_timing(result.get('latency_seconds')),
                            'transport_timings':public_timings(result.get('transport_timings')),
                            'memory':{k:memory_diagnostics[k] for k in ('injected_count','injected_bytes','budget_kind')} if memory_diagnostics else None}, ensure_ascii=False)+'\n')
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
        self.thread = threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02},daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/complete'

    @classmethod
    def from_env(cls, log_path):
        if os.environ.get('AGENTRT_MODEL_BROKER_URL'):
            from .model_broker import BrokerClient
            return cls(BrokerClient.from_env(),log_path)
        return cls(DeepSeekClient(os.environ.get('DEEPSEEK_API_KEY'),
            model=os.environ.get('DEEPSEEK_MODEL', 'deepseek-flash')), log_path)

    def issue(self,agent,*,stream_key=None):
        token=secrets.token_urlsafe(32)
        with self.lock:self.capabilities[token]={'agent':agent,'cancel':threading.Event(),'stream_key':stream_key,'preview':''}
        return token

    def revoke(self,token):
        with self.lock:
            event=self.capabilities.pop(token,None)
            if event:event['cancel'].set()

    def close(self):
        self.cancel_event.set()
        self.server.shutdown()
        with self.connection_lock:connections=list(self.connections)
        for connection in connections:
            try:connection.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        self.server.server_close()
        self.thread.join(timeout=2)
