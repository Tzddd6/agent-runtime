"""Loopback-only credential owner; keys live in RAM, independent of the UI.

The control token is not a supplier key. No API returns the supplier key.
An accepted request ID is never reused for a second inference, even after its
completed response has left the bounded response history.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .model_gateway import DeepSeekClient, ModelError, NoRedirect, MAX_BYTES


BROKER_URL='http://127.0.0.1:18886'
ACTIVE=('queued','running')


class ModelBroker:
    def __init__(self,token,client_factory=None,concurrency=2,history_limit=20,max_pending=8,max_requests=4096):
        if not isinstance(token,str) or len(token)<16:raise ValueError('Control token is required')
        if any(type(v) is not int or v<1 for v in (concurrency,history_limit,max_pending,max_requests)):
            raise ValueError('Broker bounds must be positive integers')
        self.token=token
        self.factory=client_factory or (lambda key:DeepSeekClient(key))
        self.slots=threading.BoundedSemaphore(concurrency)
        self.history_limit,self.max_pending,self.max_requests=history_limit,max_pending,max_requests
        self.lock=threading.RLock()
        self._key=''
        self.model='deepseek-flash'
        self.calls,self.receipts={},{}
        self.closed=False

    def status(self):
        with self.lock:
            return {'configured':bool(self._key),'model':self.model,
                    'busy':sum(c['status'] in ACTIVE for c in self.calls.values())}

    def configure(self,key):
        if not isinstance(key,str) or len(key)>512 or '\n' in key or '\r' in key or (key and len(key)<16):
            raise ValueError('Invalid supplier key format')
        with self.lock:
            if self.closed:raise ModelError('Model broker is closed')
            if any(c['status'] in ACTIVE for c in self.calls.values()):
                raise ModelError('Active model calls prevent changing credentials')
            self._key=key
            return {'configured':bool(key)}

    def submit(self,value):
        if not isinstance(value,dict):raise ValueError('JSON object required')
        identity=value.get('request_id')
        if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',identity):
            raise ValueError('Invalid request identity')
        messages,tokens=value.get('messages'),value.get('max_tokens',256)
        if (not isinstance(messages,list) or not messages or type(tokens) is not int or not 1<=tokens<=4096
                or any(not isinstance(m,dict) or m.get('role') not in ('system','user','assistant')
                       or not isinstance(m.get('content'),str) for m in messages)):
            raise ValueError('Invalid model request')
        raw=json.dumps({'messages':messages,'max_tokens':tokens},ensure_ascii=False).encode()
        if len(raw)>MAX_BYTES:raise ValueError('Model request exceeds 1 MiB')
        digest=hashlib.sha256(raw).hexdigest()
        with self.lock:
            if identity in self.receipts:
                previous=self.receipts[identity]
                if previous['digest']!=digest:raise ValueError('Request identity reused with different data')
                return {'call_id':previous['call_id'],'reused':True}
            if self.closed or not self._key:raise ModelError('Supplier key is not configured')
            if len(self.receipts)>=self.max_requests:raise ModelError('Request identity capacity reached; administrative rotation required')
            if sum(c['status'] in ACTIVE for c in self.calls.values())>=self.max_pending:
                raise ModelError('Model queue capacity reached')
            call_id=uuid.uuid4().hex
            cancel=threading.Event()
            call={'call_id':call_id,'status':'queued','created':time.time(),'cancel':cancel}
            self.calls[call_id]=call
            self.receipts[identity]={'call_id':call_id,'digest':digest}
            key=self._key
        # Prompt/key are transient stack data, never stored in the request ledger.
        def execute():
            entered=False
            result=None
            final='failed'
            try:
                deadline=time.monotonic()+35
                while not entered:
                    if cancel.is_set():raise ModelError('Local model call cancelled; remote billing state unknown')
                    if time.monotonic()>=deadline:raise ModelError('Model queue deadline exceeded')
                    entered=self.slots.acquire(timeout=.05)
                with self.lock:call['status']='running'
                client=self.factory(key)
                client.cancel_event=cancel
                if cancel.is_set():raise ModelError('Local model call cancelled; remote billing state unknown')
                result=client.complete(messages,tokens)
                if not isinstance(result,dict):raise ModelError('Invalid broker client response')
                result={k:v for k,v in result.items() if k in ('content','model','usage','finish_reason','latency_seconds')}
                if not isinstance(result.get('content'),str) or not result['content'].strip():
                    raise ModelError('Model broker received no usable content')
                encoded=json.dumps(result,ensure_ascii=False).encode()
                if len(encoded)>2*MAX_BYTES:raise ModelError('Model response exceeds broker limit')
                final='cancelled' if cancel.is_set() else 'succeeded'
            except Exception:
                # An arbitrary injected client exception may contain secrets.
                # Do not retain its text, traceback, prompt, headers, or key.
                result={'error':'Model call cancelled or failed; remote billing state may be unknown'}
                final='cancelled' if cancel.is_set() else 'failed'
            finally:
                if entered:self.slots.release()
                with self.lock:
                    call.update(status=final,result=result,finished=time.time())
                    completed=[k for k,c in self.calls.items() if c['status'] not in ACTIVE]
                    for old in completed[:-self.history_limit]:self.calls.pop(old,None)
        threading.Thread(target=execute,name='agentrt-model-call',daemon=True).start()
        return {'call_id':call_id,'reused':False}

    def detail(self,identity):
        with self.lock:
            if identity not in self.calls:raise KeyError(identity)
            return {k:v for k,v in self.calls[identity].items() if k!='cancel'}

    def cancel(self,identity):
        with self.lock:
            call=self.calls.get(identity)
            if call is not None:call['cancel'].set()
            return {'cancel_requested':call is not None}

    def close(self):
        with self.lock:
            self.closed=True
            self._key=''
            for call in self.calls.values():call['cancel'].set()


def make_server(broker,port=18886):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(3)
            super().setup()
        def log_message(self,*args):pass
        def respond(self,code,value):
            raw=json.dumps(value,ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type','application/json')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            try:self.wfile.write(raw)
            except OSError:pass
        def authorized(self):
            if not secrets.compare_digest(self.headers.get('X-AgentRT-Proxy-Token',''),broker.token):
                self.respond(403,{'error':'Broker authorization required'});return False
            return True
        def do_GET(self):
            if not self.authorized():return
            if self.path=='/status':self.respond(200,broker.status())
            elif re.fullmatch(r'/calls/[a-f0-9]{32}',self.path):
                try:self.respond(200,broker.detail(self.path.rsplit('/',1)[1]))
                except KeyError:self.respond(410,{'error':'Response unavailable; request must not be resubmitted'})
            else:self.respond(404,{'error':'Not found'})
        def do_POST(self):
            if not self.authorized():return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=MAX_BYTES:raise ValueError('Request size invalid')
                value=json.loads(self.rfile.read(length))
                if self.path=='/configure':self.respond(200,broker.configure(value.get('key')))
                elif self.path=='/calls':self.respond(202,broker.submit(value))
                elif re.fullmatch(r'/calls/[a-f0-9]{32}/cancel',self.path):
                    self.respond(200,broker.cancel(self.path.split('/')[2]))
                else:self.respond(404,{'error':'Not found'})
            except (ValueError,TypeError,AttributeError):self.respond(400,{'error':'Invalid broker request'})
            except ModelError:self.respond(409,{'error':'Model broker unavailable, busy, or capacity reached'})
            except OSError:pass
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads=True
    return server


class BrokerClient:
    def for_request(self,cancel_event):
        import copy
        client=copy.copy(self);client.cancel_event=cancel_event
        return client

    def __init__(self,token,url=BROKER_URL,*,local_test=False):
        from urllib.parse import urlparse
        parsed=urlparse(url)
        if (url!=BROKER_URL and not(local_test and parsed.scheme=='http' and parsed.hostname=='127.0.0.1'
                                    and parsed.path=='' and not parsed.username and not parsed.password)):
            raise ModelError('Only the fixed loopback model broker is permitted')
        if not token:raise ModelError('Broker control token is missing')
        self.token,self.url=token,url
        self.model,self.timeout='deepseek-flash',40
        self.cancel_event=None

    @classmethod
    def from_env(cls):
        return cls(os.environ.get('AGENTRT_PROXY_TOKEN',''),os.environ.get('AGENTRT_MODEL_BROKER_URL',BROKER_URL))

    def _request(self,path,body=None):
        headers={'X-AgentRT-Proxy-Token':self.token}
        data=None if body is None else json.dumps(body,ensure_ascii=False).encode()
        request=urllib.request.Request(self.url+path,data=data,headers=headers)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=2) as response:
                raw=response.read(2*MAX_BYTES+2049)
            if len(raw)>2*MAX_BYTES+2048:raise ModelError('Broker response exceeds limit')
            return json.loads(raw)
        except (OSError,ValueError):raise ModelError('Local model broker request failed; no paid POST retry') from None

    def status(self):return self._request('/status')
    def configure(self,key):return self._request('/configure',{'key':key})

    def complete(self,messages,max_tokens=256):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise ModelError('Local model call cancelled')
        accepted=self._request('/calls',{'request_id':uuid.uuid4().hex,'messages':messages,'max_tokens':max_tokens})
        identity=accepted['call_id']
        deadline=time.monotonic()+self.timeout
        try:
            while time.monotonic()<deadline:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise ModelError('Local model call cancelled; remote billing state unknown')
                try:call=self._request('/calls/'+identity)
                except ModelError:
                    time.sleep(.1);continue # Read-only polling can be retried.
                if call['status']=='succeeded':return call['result']
                if call['status'] not in ACTIVE:raise ModelError('Model broker call failed or cancelled; remote billing state unknown')
                time.sleep(.1)
            raise ModelError('Model broker deadline exceeded; remote billing state unknown')
        except BaseException:
            try:self._request('/calls/'+identity+'/cancel',{})
            except ModelError:pass
            raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=18886)
    args=parser.parse_args()
    if args.port!=18886:parser.error('Production broker port is fixed at 18886')
    broker=ModelBroker(os.environ.get('AGENTRT_PROXY_TOKEN',''))
    server=make_server(broker,args.port)
    try:server.serve_forever()
    finally:broker.close();server.server_close()


if __name__=='__main__':main()
