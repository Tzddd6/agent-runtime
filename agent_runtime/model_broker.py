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
MULTI_BROKER_URL='http://127.0.0.1:18887'
FEDERATED_BROKER_URL='http://127.0.0.1:18888'
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

    def snapshot_client(self,value):
        if value.get('profile_id') is not None:raise ValueError('This broker does not support profiles')
        if not self._key:raise ModelError('Supplier key is not configured')
        key,factory=self._key,self.factory
        return lambda:factory(key),{}

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
        request_body={'messages':messages,'max_tokens':tokens}
        if 'profile_id' in value:
            if not isinstance(value['profile_id'],str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',value['profile_id']):raise ValueError('Invalid profile identity')
            request_body['profile_id']=value['profile_id']
        if 'profile_revision' in value:
            if type(value['profile_revision']) is not int or value['profile_revision']<1:raise ValueError('Invalid profile revision')
            request_body['profile_revision']=value['profile_revision']
        raw=json.dumps(request_body,ensure_ascii=False).encode()
        if len(raw)>MAX_BYTES:raise ValueError('Model request exceeds 1 MiB')
        digest=hashlib.sha256(raw).hexdigest()
        with self.lock:
            if identity in self.receipts:
                previous=self.receipts[identity]
                if previous['digest']!=digest:raise ValueError('Request identity reused with different data')
                return {'call_id':previous['call_id'],'reused':True}
            if self.closed:raise ModelError('Model broker is closed')
            if len(self.receipts)>=self.max_requests:raise ModelError('Request identity capacity reached; administrative rotation required')
            if sum(c['status'] in ACTIVE for c in self.calls.values())>=self.max_pending:
                raise ModelError('Model queue capacity reached')
            client_factory,metadata=self.snapshot_client(value)
            call_id=uuid.uuid4().hex
            cancel=threading.Event()
            call={'call_id':call_id,'status':'queued','created':time.time(),'cancel':cancel,**metadata}
            self.calls[call_id]=call
            self.receipts[identity]={'call_id':call_id,'digest':digest}
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
                client=client_factory()
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
            elif self.path=='/profiles' and hasattr(broker,'profiles_info'):self.respond(200,broker.profiles_info())
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
                elif self.path=='/profiles/save' and hasattr(broker,'save_profile'):self.respond(200,broker.save_profile(value))
                elif self.path=='/profiles/select' and hasattr(broker,'select_profile'):self.respond(200,broker.select_profile(value))
                elif self.path=='/profiles/remove' and hasattr(broker,'remove_profile'):self.respond(200,broker.remove_profile(value))
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
        if (url not in (BROKER_URL,MULTI_BROKER_URL,FEDERATED_BROKER_URL) and not(local_test and parsed.scheme=='http' and parsed.hostname=='127.0.0.1'
                                    and parsed.path=='' and not parsed.username and not parsed.password)):
            raise ModelError('Only the fixed loopback model broker is permitted')
        if not token:raise ModelError('Broker control token is missing')
        self.token,self.url=token,url
        self.model,self.timeout='deepseek-flash',40
        self.cancel_event=None
        self.supports_stream_callbacks=os.environ.get('AGENTRT_PROVIDER_STREAMING')=='1'

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
    def profiles_info(self):return self._request('/profiles')
    def save_profile(self,value):return self._request('/profiles/save',value)
    def select_profile(self,value):return self._request('/profiles/select',value)
    def remove_profile(self,value):return self._request('/profiles/remove',value)

    def for_profile(self,identity,model=None):
        if not isinstance(identity,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',identity):raise ValueError('Invalid profile identity')
        import copy
        client=copy.copy(self)
        for field in ('profile_id','profile_revision'):
            if hasattr(client,field):delattr(client,field)
        if identity!='legacy':
            row=next((p for p in self.profiles_info()['profiles'] if p['id']==identity),None)
            if row is None or not row['configured']:raise ModelError('API profile unavailable')
            if model is not None and model!=row['model']:raise ModelError('API profile model changed before workflow admission')
            client.profile_revision=row['revision'];model=row['model']
            if self.url==FEDERATED_BROKER_URL and row.get('upstream') is True:client.url=MULTI_BROKER_URL
        if not(identity=='legacy' and self.url==BROKER_URL):client.profile_id=identity
        if model is not None:client.model=model
        return client

    def complete(self,messages,max_tokens=256):
        started=time.monotonic()
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise ModelError('Local model call cancelled')
        profile=getattr(self,'profile_id',None)
        if self.url in (MULTI_BROKER_URL,FEDERATED_BROKER_URL) and profile is None:
            info=self.status();profile=info.get('profile_id')
            revision=info.get('profile_revision')
        else:revision=getattr(self,'profile_revision',None)
        if self.url in (MULTI_BROKER_URL,FEDERATED_BROKER_URL) and profile=='legacy':
            # Keep the old RAM owner and its exactly-once receipt path, without
            # adding another paid submission/polling hop around it.
            import copy
            legacy=copy.copy(self);legacy.url=BROKER_URL
            for field in ('profile_id','profile_revision'):
                if hasattr(legacy,field):delattr(legacy,field)
            return legacy.complete(messages,max_tokens)
        body={'request_id':uuid.uuid4().hex,'messages':messages,'max_tokens':max_tokens}
        if profile is not None:body['profile_id']=profile
        if revision is not None:body['profile_revision']=revision
        accepted=self._request('/calls',body)
        identity=accepted['call_id']
        deadline=time.monotonic()+self.timeout
        reads=0
        try:
            while time.monotonic()<deadline:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise ModelError('Local model call cancelled; remote billing state unknown')
                try:
                    call=self._request('/calls/'+identity)
                    reads+=1
                except ModelError:
                    time.sleep(.1);continue # Read-only polling can be retried.
                if call['status']=='succeeded':
                    result=dict(call['result'])
                    result['transport_timings']={
                        'client_seconds':time.monotonic()-started,
                        'broker_seconds':max(0,call['finished']-call['created']),
                        'completion_detection_seconds':max(0,time.time()-call['finished']),
                        'status_reads':reads}
                    return result
                if call['status'] not in ACTIVE:raise ModelError('Model broker call failed or cancelled; remote billing state unknown')
                time.sleep(.05)
            raise ModelError('Model broker deadline exceeded; remote billing state unknown')
        except BaseException:
            try:self._request('/calls/'+identity+'/cancel',{})
            except ModelError:pass
            raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=18886)
    args=parser.parse_args()
    if args.port not in (18886,18887,18888):parser.error('Production broker ports are fixed loopback endpoints')
    token=os.environ.get('AGENTRT_PROXY_TOKEN','')
    if args.port==18888:
        from .federated_profiles import FederatedProfiles
        broker=FederatedProfiles(token,upstream=BrokerClient(token,MULTI_BROKER_URL),legacy_client=BrokerClient(token,BROKER_URL))
    elif args.port==18887:
        from .model_profiles import MultiModelBroker
        broker=MultiModelBroker(token,legacy_client=BrokerClient(token,BROKER_URL))
    else:broker=ModelBroker(token)
    server=make_server(broker,args.port)
    try:server.serve_forever()
    finally:broker.close();server.server_close()


if __name__=='__main__':main()
