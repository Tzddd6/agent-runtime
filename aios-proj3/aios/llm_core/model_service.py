"""RAM-only concurrent provider service. Worker subprocesses never inherit supplier keys."""
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from queue import Empty, Queue
from threading import Lock, Thread
from uuid import uuid4

from cerebrum.llm.apis import LLMQuery, LLMResponse


def serve(connection):
    from aios.llm_core.adapter import LLMAdapter
    from aios.syscall.llm import LLMSyscall

    adapter = LLMAdapter(llm_configs=[{'name': 'mock-aios-core', 'backend': 'mock'}])
    broker = adapter.profile_broker
    send_lock = Lock()
    pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix='aios-ms-worker')

    def send_frame(req_id, kind, payload):
        with send_lock:
            try:
                connection.send((req_id, kind, payload))
            except (OSError, BrokenPipeError, EOFError):
                pass

    def handle_complete(req_id, value):
        try:
            if value.get('model') != 'mock-aios-core' and not broker.status()['configured']:
                raise ValueError('Provider is not configured')
            query = LLMQuery(**value)
            query.stream_observer = lambda event: send_frame(req_id, 'delta', event)
            # Use a lightweight per-request adapter view sharing the same RAM-only profile_broker
            req_adapter = LLMAdapter(
                llm_configs=adapter.llm_configs,
                log_mode='silent',
                use_context_manager=False,
            )
            req_adapter.profile_broker = broker
            req_adapter.stream_observer = lambda event: send_frame(req_id, 'delta', event)
            response = req_adapter.execute_llm_syscall(LLMSyscall('provider', query))
            send_frame(req_id, 'result', response.to_dict())
        except Exception:
            send_frame(req_id, 'error', 'Model service operation failed')

    try:
        while True:
            req_id, operation, value = connection.recv()
            if operation == 'close':
                return
            if operation == 'complete':
                pool.submit(handle_complete, req_id, value)
            else:
                try:
                    methods = {
                        'status': broker.status,
                        'profiles': broker.profiles_info,
                        'save': lambda: broker.save_profile(value),
                        'select': lambda: broker.select_profile(value['id']),
                        'remove': lambda: broker.remove_profile(value['id']),
                    }
                    send_frame(req_id, 'result', methods[operation]())
                except Exception:
                    send_frame(req_id, 'error', 'Model service operation failed')
    except EOFError:
        pass
    finally:
        pool.shutdown(wait=False)
        broker.profiles.clear()
        broker._legacy_key = ''
        try:
            connection.close()
        except OSError:
            pass


class ModelService:
    """Concurrent multiplexed RAM-only key-holding provider process."""

    def __init__(self):
        context = get_context('spawn')
        self.connection, child = context.Pipe()
        self.send_lock = Lock()
        self.state_lock = Lock()
        self._pending = {}
        self.process = context.Process(target=serve, args=(child,), daemon=True)
        self.process.start()
        child.close()
        self.closed = False
        self._reader = Thread(target=self._read_loop, name='aios-ms-reader', daemon=True)
        self._reader.start()

    def _fail_all_pending(self, reason='Model service unavailable'):
        with self.state_lock:
            queues = list(self._pending.values())
            self._pending.clear()
        for q in queues:
            q.put(('fatal', reason))

    def _read_loop(self):
        try:
            while True:
                req_id, kind, payload = self.connection.recv()
                with self.state_lock:
                    q = self._pending.get(req_id)
                if q is not None:
                    q.put((kind, payload))
        except (EOFError, OSError, ValueError):
            self._fail_all_pending()

    def request(self, operation, value=None, observer=None):
        req_id = uuid4().hex
        q = Queue()
        with self.state_lock:
            if self.closed or not self.process.is_alive():
                raise RuntimeError('Model service unavailable')
            self._pending[req_id] = q
        try:
            with self.send_lock:
                self.connection.send((req_id, operation, value))
            deadline = 120.0
            while True:
                try:
                    kind, result = q.get(timeout=deadline)
                except Empty:
                    self.close()
                    raise TimeoutError('Model service deadline exceeded; request was not replayed')
                if kind == 'delta':
                    if observer:
                        try:
                            observer(result)
                        except Exception:
                            pass
                elif kind == 'result':
                    return result
                elif kind == 'fatal':
                    raise RuntimeError(result)
                else:
                    raise ValueError('模型服务未确认操作，未自动重试')
        finally:
            with self.state_lock:
                self._pending.pop(req_id, None)

    def complete(self, query):
        raw = self.request(
            'complete',
            {key: value for key, value in query.to_dict().items() if key in LLMQuery.__dataclass_fields__},
            observer=getattr(query, 'stream_observer', None),
        )
        return LLMResponse(**{key: value for key, value in raw.items() if key in LLMResponse.__dataclass_fields__})

    def close(self):
        with self.state_lock:
            if self.closed:
                return
            self.closed = True
        try:
            if self.process.is_alive():
                with self.send_lock:
                    self.connection.send(('__close__', 'close', None))
                self.process.join(timeout=3)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=3)
        except (OSError, BrokenPipeError, ValueError):
            pass
        finally:
            self._fail_all_pending('Model service closed')
            try:
                self.connection.close()
            except OSError:
                pass
