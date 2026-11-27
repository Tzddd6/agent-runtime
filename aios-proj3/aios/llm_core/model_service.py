"""RAM-only concurrent provider service. Worker subprocesses never inherit supplier keys."""
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from queue import Empty, Queue
from threading import Lock, Thread
from time import monotonic, time
from uuid import uuid4

from aios.llm_core.diagnostics import MESSAGES, check_model_list
from aios.llm_core.profiles import connection_route, model_suggestion
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

    def test_connection(req_id, config):
        try:
            issue = check_model_list(config) if config else 'unconfigured'
            result = {'ok': issue == 'available', 'code': issue, 'message': MESSAGES[issue],
                      'model': config['name'] if config else None, 'checked_at': time()}
            if config:
                result.update(profile_id=config['profile_id'], endpoint=config['hostname'],
                              transport=connection_route(config['hostname']),
                              suggested_model=model_suggestion(config['backend'], config['name']))
                broker.record_connection(config, result)
            send_frame(req_id, 'result', result)
        except Exception:
            send_frame(req_id, 'error', 'Model service operation failed')

    def handle_complete(req_id, value):
        try:
            requested = value.get('model')
            profile = None if requested == 'mock-aios-core' else broker.model_config()
            if requested != 'mock-aios-core':
                issue = 'unconfigured' if profile is None else 'model' if requested not in (None, profile['name']) else None
                if issue:
                    response = LLMResponse(status_code=503 if issue == 'unconfigured' else 400,
                                           error=MESSAGES[issue], error_code=issue)
                    send_frame(req_id, 'result', response.to_dict())
                    return
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
            syscall = LLMSyscall('provider', query)
            if profile is not None:
                syscall.selected_model_config = profile
            response = req_adapter.execute_llm_syscall(syscall)
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
            elif operation == 'test_connection':
                profile_id = value.get('id') if isinstance(value, dict) else None
                if profile_id is not None and not isinstance(profile_id, str):
                    send_frame(req_id, 'error', 'Invalid profile identifier')
                    continue
                pool.submit(test_connection, req_id, broker.model_config(profile_id))
            else:
                try:
                    methods = {
                        'status': broker.status,
                        'profiles': broker.profiles_info,
                        'save': lambda: broker.save_profile(value),
                        'save-and-select': lambda: broker.save_and_select(value),
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


class ModelServiceUnavailable(RuntimeError):
    pass


class ModelService:
    """Concurrent multiplexed RAM-only key-holding provider process."""

    def __init__(self, request_timeout=120.0):
        self.request_timeout = request_timeout
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
                raise ModelServiceUnavailable('Model service unavailable')
            self._pending[req_id] = q
        try:
            try:
                with self.send_lock:
                    self.connection.send((req_id, operation, value))
            except (OSError, EOFError) as exc:
                raise ModelServiceUnavailable('Model service connection lost') from exc
            deadline = monotonic() + self.request_timeout
            while True:
                try:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise Empty
                    kind, result = q.get(timeout=remaining)
                except Empty:
                    raise TimeoutError('Model service deadline exceeded; request was not replayed') from None
                if kind == 'delta':
                    if observer:
                        try:
                            observer(result)
                        except Exception:
                            pass
                elif kind == 'result':
                    return result
                elif kind == 'fatal':
                    raise ModelServiceUnavailable(result)
                else:
                    raise ValueError('模型服务未确认操作，未自动重试')
        finally:
            with self.state_lock:
                self._pending.pop(req_id, None)

    def complete(self, query):
        try:
            raw = self.request(
                'complete',
                {key: value for key, value in query.to_dict().items() if key in LLMQuery.__dataclass_fields__},
                observer=getattr(query, 'stream_observer', None),
            )
        except TimeoutError:
            return LLMResponse(status_code=504, error=MESSAGES['request_timeout'], error_code='request_timeout')
        except ModelServiceUnavailable:
            return LLMResponse(status_code=503, error=MESSAGES['service_unavailable'], error_code='service_unavailable')
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
