"""Workflow-local authenticated mailboxes. Large bodies live in ContextStore.

Capabilities expire at Attempt completion. This is API isolation, not an OS sandbox.
"""
import collections
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Broker:
    def __init__(self, store, agents, capacity=128):
        self.store, self.capacity = store, capacity
        self.agents = set(agents)
        self.tokens, self.mailboxes, self.receipts = {}, {}, {}
        self.lock = threading.RLock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.connection.settimeout(5)
                try:
                    size = int(self.headers.get('Content-Length', 0))
                    if self.path != '/rpc' or not 0 < size <= 1024 * 1024:
                        raise ValueError('invalid RPC request')
                    token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                    value = json.loads(self.rfile.read(size))
                    result = owner.dispatch(token, value)
                    code = 200
                except PermissionError:
                    code, result = 403, {'error': 'unauthorized capability or recipient'}
                except (ValueError, TypeError, KeyError, AttributeError, OSError):
                    code, result = 400, {'error': 'invalid RPC or mailbox capacity exceeded'}
                raw = json.dumps(result, ensure_ascii=False).encode('utf-8')
                try:
                    self.send_response(code)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/rpc'

    def issue(self, agent):
        with self.lock:
            self.agents.add(agent)
            token = secrets.token_urlsafe(32)
            self.tokens[token] = agent
            return token

    def revoke(self, token):
        with self.lock:
            self.tokens.pop(token, None)

    def dispatch(self, token, value):
        with self.lock:
            agent = self.tokens.get(token)
            if agent is None:
                raise PermissionError()
            op = value['op']
            if op == 'send':
                peer, request = value['to'], value['request_id']
                if peer not in self.agents:
                    raise PermissionError()
                if not isinstance(request, str) or not 1 <= len(request) <= 128:
                    raise ValueError('invalid request_id')
                identity = (agent, request)
                # Cache preserves accepted request identities for the whole workflow.
                previous = self.receipts.get(identity)
                if previous is not None:
                    if previous['to'] != peer or self.store.get(previous['ref'], agent) != value['data']:
                        raise ValueError('request_id reused with different body')
                    return previous
                mailbox = self.mailboxes.setdefault(peer, collections.deque())
                if len(mailbox) >= self.capacity or len(self.receipts) >= 4096:
                    raise ValueError('capacity exceeded')
                ref = self.store.put(value['data'], [agent, peer])
                receipt = {'from': agent, 'to': peer, 'request_id': request, 'ref': ref}
                mailbox.append(receipt)
                self.receipts[identity] = receipt
                return receipt
            if op == 'receive':
                mailbox = self.mailboxes.setdefault(agent, collections.deque())
                # Read/ack gives at-least-once delivery until explicit acknowledgement.
                return {'message': mailbox[0] if mailbox else None}
            if op == 'ack':
                mailbox = self.mailboxes.setdefault(agent, collections.deque())
                if mailbox and mailbox[0]['ref'] == value['ref'] and mailbox[0]['request_id'] == value['request_id']:
                    mailbox.popleft()
                return {'ok': True}
            if op == 'get':
                return {'data': self.store.get(value['ref'], agent)}
            raise ValueError('unknown operation')

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
