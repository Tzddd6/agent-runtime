import threading
import socket
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler
from agent_runtime.local_server import LocalHTTPServer


class LocalServerTests(unittest.TestCase):
    def test_partial_headers_cannot_hold_teardown_forever(self):
        entered=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def setup(self):super().setup();entered.set()
            def log_message(self,*args):pass
        server=LocalHTTPServer(('127.0.0.1',0),Handler);server.request_timeout=.1
        thread=threading.Thread(target=server.serve_forever);thread.start()
        client=socket.create_connection(server.server_address,timeout=2);client.sendall(b'GET / HTTP/1.1\r\n')
        self.assertTrue(entered.wait(2));server.shutdown()
        closer=threading.Thread(target=server.server_close);closer.start();closer.join(2)
        client.close();thread.join(2);self.assertFalse(closer.is_alive())

    def test_shutdown_wakes_an_idle_server_without_polling(self):
        server=LocalHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':3600})
        thread.start()
        closer=threading.Thread(target=server.shutdown);closer.start();closer.join(2)
        self.assertFalse(closer.is_alive(),'Shutdown waited for polling deadline')
        thread.join(2);self.assertFalse(thread.is_alive());server.server_close()

    def test_shutdown_joins_an_inflight_handler(self):
        entered=threading.Event();release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                entered.set();release.wait(3)
                self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'ok')
        server=LocalHTTPServer(('127.0.0.1',0),Handler)
        server.daemon_threads=False
        thread=threading.Thread(target=server.serve_forever);thread.start()
        result=[]
        def read():
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:'+str(server.server_port),timeout=5) as r:result.append(r.read())
        client=threading.Thread(target=read);client.start();self.assertTrue(entered.wait(2))
        server.shutdown();closed=threading.Event()
        def close():server.server_close();closed.set()
        closer=threading.Thread(target=close);closer.start()
        self.assertFalse(closed.wait(.05),'Handler was abandoned during close')
        release.set();closer.join(2);client.join(2);thread.join(2)
        self.assertTrue(closed.is_set());self.assertEqual(result,[b'ok'])
