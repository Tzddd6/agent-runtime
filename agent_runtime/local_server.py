"""Loopback HTTP service with an explicit shutdown wakeup, not idle polling.

Handler isolation, request limits and worker cleanup remain with the callers.
The private socketpair never accepts network input or carries Agent data.
"""
import selectors
import socket
import threading
from http.server import ThreadingHTTPServer


class LocalHTTPServer(ThreadingHTTPServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wake_read, self._wake_write = socket.socketpair()
        self._stop_requested = threading.Event()
        self._stopped = threading.Event()
        self._serving_thread = None
        self._wake_closed = False

    def serve_forever(self, poll_interval=None):
        # poll_interval is accepted for BaseServer compatibility, not used.
        self._serving_thread = threading.get_ident()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self, selectors.EVENT_READ)
                selector.register(self._wake_read, selectors.EVENT_READ)
                while not self._stop_requested.is_set():
                    for key, _ in selector.select():
                        if self._stop_requested.is_set():
                            break
                        if key.fileobj is self:
                            self._handle_request_noblock()
        finally:
            self._stopped.set()

    def shutdown(self):
        self._stop_requested.set()
        try:
            self._wake_write.send(b'x')
        except OSError:
            pass
        if self._serving_thread != threading.get_ident():
            self._stopped.wait()

    def server_close(self):
        super().server_close()  # Retain ThreadingMixIn's handler joins.
        if not self._wake_closed:
            self._wake_closed = True
            self._wake_read.close()
            self._wake_write.close()
