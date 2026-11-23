"""Loopback console backed by the native AIOS workflow engine."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
from threading import Lock
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from aios.os_kernel.telemetry import MachineSampler
from aios.storage.database import RuntimeDatabase
from aios.storage.knowledge import KnowledgeStore
from aios.storage.attachments import AttachmentStore
from aios.llm_core.model_service import ModelService
from runtime.conversation import Conversations
from aios.tool.catalog import get_catalog
from aios.tool.bindings import BOUND_TOOLS
from aios.tool.virtual_env.desktop_env import get_vm_controller
from cerebrum.hub import AgentHub, AgentManifest
from cerebrum.memory.apis import MemoryQuery
from cerebrum.tool.apis import ToolQuery
from runtime.launch import AIOSKernelRuntime

STATIC = Path(__file__).with_name('static').resolve()


def _default_agent_hub() -> AgentHub:
    hub = AgentHub()
    builtin_apps = [
        AgentManifest(
            app_id='aios/code_auditor',
            name='工程审计与修复代理',
            version='1.0.0',
            author='aios-core',
            description='在受控工作区中执行代码定位、补丁生成与七阶段单元测试验证。',
            required_syscalls=['llm', 'storage', 'tool'],
            resource_quota={'cpu_milli': 500, 'memory_bytes': 128 * 1024 * 1024, 'pids': 8, 'max_concurrent': 2, 'max_model_calls': 20, 'max_tokens': 50_000},
        ),
        AgentManifest(
            app_id='aios/memory_curator',
            name='A-Mem 卡片盒记忆整理代理',
            version='1.0.0',
            author='aios-core',
            description='自动为记忆笔记提取标签、建立双向语义链接并演化邻居摘要。',
            required_syscalls=['memory', 'llm'],
            resource_quota={'cpu_milli': 250, 'memory_bytes': 64 * 1024 * 1024, 'pids': 4, 'max_concurrent': 2, 'max_model_calls': 10, 'max_tokens': 20_000},
        ),
        AgentManifest(
            app_id='aios/lsfs_navigator',
            name='LSFS 自然语言文件助手',
            version='1.0.0',
            author='aios-core',
            description='通过 IntentRouter 将自然语言指令转译为带版本回滚与安全共享的 StorageQuery。',
            required_syscalls=['storage'],
            resource_quota={'cpu_milli': 250, 'memory_bytes': 64 * 1024 * 1024, 'pids': 4, 'max_concurrent': 2, 'max_model_calls': 0, 'max_tokens': 0},
        ),
        AgentManifest(
            app_id='aios/litecua_operator',
            name='LiteCUA 桌面操作代理',
            version='1.0.0',
            author='aios-core',
            description='以 MCP 工具形式观察虚拟桌面无障碍树、执行点击/输入/快捷键与快照回滚。',
            required_syscalls=['tool', 'memory'],
            allowed_tools=['litecua/observe', 'litecua/click', 'litecua/type', 'litecua/hotkey', 'litecua/snapshot'],
            resource_quota={'cpu_milli': 500, 'memory_bytes': 128 * 1024 * 1024, 'pids': 6, 'max_concurrent': 1, 'max_model_calls': 10, 'max_tokens': 20_000},
        ),
    ]
    for manifest in builtin_apps:
        hub.publish(manifest, lambda client, params, app_id=manifest.app_id: {'app_id': app_id, 'status': 'ready', 'params': params})
    return hub


class NativeConsole:
    def __init__(self, kernel, root):
        self.kernel, self.root = kernel, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.csrf = secrets.token_urlsafe(32)
        self.lock = Lock()
        self.jobs = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='aios-web')
        self.database = RuntimeDatabase(self.root / 'database')
        self.knowledge = KnowledgeStore(self.database)
        self.attachments = AttachmentStore(self.root / 'attachments', self.database)
        self.model_service = ModelService()
        self.kernel.llm.model_service = self.model_service
        self.telemetry = MachineSampler(kernel.cgroup_manager.root_path if kernel.cgroup_manager.enabled else None)
        self.vm = get_vm_controller()
        self.agent_hub = _default_agent_hub()
        self.conversations = Conversations(self)
        for path in sorted(self.root.glob('*.json'), key=lambda path: path.stat().st_mtime)[-20:]:
            if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                continue
            try:
                job = json.loads(path.read_text(encoding='utf-8'))
                if job['id'] != path.stem:
                    continue
                if job['status'] == 'running':
                    job['status'] = 'interrupted'
                self.jobs[job['id']] = job
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def close(self):
        self.pool.shutdown(wait=True)
        self.model_service.close()

    def detail(self, identity):
        with self.lock:
            job = deepcopy(self.jobs[identity])
        graph = next((graph for graph in self.kernel.workflow_engine.recent_graphs() if graph['id'] == identity), None)
        if graph and job['status'] == 'running':
            job['states'] = {node['id']: node['state'] for node in graph['nodes']}
            job['attempts'] = {node['id']: node['attempts'] for node in graph['nodes']}
        return job

    def run_litecua(self, value):
        action = value.get('action')
        if action == 'observe':
            tool_call = {'name': 'litecua/observe', 'parameters': {}}
        elif action == 'click_search':
            tool_call = {'name': 'litecua/click', 'parameters': {'element_id': 'input_search_bar'}}
        elif action == 'type_query':
            text = value.get('text', 'AIOS syscall')
            if not isinstance(text, str) or len(text) > 500:
                raise ValueError('输入文本长度无效')
            tool_call = {'name': 'litecua/type', 'parameters': {'element_id': 'input_search_bar', 'text': text, 'append': False}}
        elif action == 'hotkey_run':
            tool_call = {'name': 'litecua/hotkey', 'parameters': {'keys': ['ctrl', 'shift', 'f']}}
        elif action == 'restore_boot':
            tool_call = {'name': 'litecua/snapshot', 'parameters': {'action': 'restore', 'name': 'boot'}}
        else:
            raise ValueError('不支持的桌面操作')
        result = self.kernel.execute_request('console-litecua', ToolQuery(tool_calls=[tool_call]))
        response = result['response']
        if response.status_code != 200:
            raise ValueError(response.error or '桌面操作执行失败')
        return {'action': action, 'result': response.response_message, 'state': self.vm.observe()}

    def submit(self, value):
        preset = value.get('preset')
        if preset not in ('dynamic', 'document', 'kernel', 'resources', 'agents', 'ipc', 'litecua', 'llm', 'team', 'engineering'):
            raise ValueError('不支持的任务场景')
        workers = value.get('workers', 2)
        if type(workers) is not int or not 1 <= workers <= 4:
            raise ValueError('并发数需为 1 至 4')
        text = value.get('prompt', '')
        if not isinstance(text, str) or len(text) > 4000:
            raise ValueError('文本最多 4000 字')
        with self.lock:
            if any(job['status'] == 'running' for job in self.jobs.values()):
                raise ValueError('请等待当前任务结束')
            if len(self.jobs) >= 20:
                oldest = next(iter(self.jobs))
                (self.root / (oldest + '.json')).unlink(missing_ok=True)
                self.jobs.pop(oldest)
            identity = uuid4().hex
            job = {'id': identity, 'preset': preset, 'policy': 'native_drf', 'status': 'running',
                   'created': time.time(), 'states': {}, 'attempts': {}, 'events': []}
            self.jobs[identity] = job
            try:
                self.save(job)
                if preset in ('llm', 'team', 'engineering'):
                    if not self.model_service.request('status')['configured']:
                        raise ValueError('请先配置模型服务')
                    self.pool.submit(self.execute_conversation, identity, value)
                else:
                    self.pool.submit(self.execute, identity, preset, text, workers)
            except Exception:
                self.jobs.pop(identity)
                raise
        return {'id': identity}

    def save(self, job):
        path = self.root / (job['id'] + '.json')
        temporary = path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(job, stream, ensure_ascii=False, default=lambda o: '<callable>' if callable(o) else str(o))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def execute_conversation(self, identity, value):
        try:
            self.conversations.submit({'conversation_id': 'job_' + identity, 'request_id': identity,
                                       'mode': value['preset'], 'project': value.get('project', 'pricing'),
                                       'prompt': value.get('prompt') or '完成当前受控任务'}, workflow_id=identity)
        except Exception:
            with self.lock:
                self.jobs[identity].update(status='failed', error='执行未完成，未自动重提模型请求')
                self.save(self.jobs[identity])

    def execute(self, identity, preset, text, workers):
        def summary(task, upstream, attempt):
            return {'summary': upstream}

        if preset in ('kernel', 'resources', 'agents', 'ipc', 'litecua'):
            from runtime.scenarios import tasks as native_tasks
            tasks = native_tasks(self.kernel, preset, identity, text)
        elif preset == 'document':
            tasks = [{'id': 'stats', 'query_type': 'tool', 'query_data': {'tool_calls': [
                {'name': 'text.stats', 'parameters': {'text': text}}]}},
                {'id': 'report', 'depends_on': ['stats'], 'handler': summary}]
        else:
            def branch(*_):
                time.sleep(0.15)
                return {'checked': True}

            def broken(*_):
                raise ValueError('Synthetic isolated branch failure')

            tasks = [{'id': 'plan', 'handler': lambda *_: {'ready': True}, 'spawn_tasks': [
                {'id': 'extra', 'handler': branch, 'wire_into': ['report']}]},
                {'id': 'analysis', 'depends_on': ['plan'], 'handler': branch},
                {'id': 'probe', 'depends_on': ['plan'], 'handler': broken},
                {'id': 'probe_report', 'depends_on': ['probe'], 'handler': summary},
                {'id': 'report', 'depends_on': ['analysis'], 'handler': summary}]
        try:
            result = self.kernel.workflow_engine.run_workflow(identity, tasks, max_workers=workers)
            status = 'completed_with_failures' if any(state in ('failed', 'unschedulable') for state in result['states'].values()) else 'succeeded'
            with self.lock:
                self.jobs[identity].update(result, status=status, finished=time.time())
        except Exception:
            with self.lock:
                self.jobs[identity].update(status='failed', error='任务未完成，请核对内核记录', finished=time.time())
        finally:
            with self.lock:
                self.save(self.jobs[identity])


def create_server(console, port=18892):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, code, value, media='application/json; charset=utf-8'):
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', media)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(raw)

        def local(self):
            self.connection.settimeout(5)
            if self.headers.get('Host', '').lower() not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
                self.respond(403, {'error': '仅接受本机地址'})
                return False
            return True

        def do_GET(self):
            if not self.local():
                return
            path = urlsplit(self.path).path
            kernel = console.kernel
            if path == '/':
                html = (STATIC / 'chat.html').read_text(encoding='utf-8')
                html = html.replace('/static/chat.js', '/static/native-chat.js').replace('</head>', '<script src="/static/vendor/katex/katex.min.js" defer></script><script src="/static/markdown.js" defer></script><link rel="stylesheet" href="/static/vendor/katex/katex.min.css"></head>')
                return self.respond(200, html.encode(), 'text/html; charset=utf-8')
            if path in ('/console', '/console/'):
                html = (STATIC / 'console.html').read_text(encoding='utf-8')
                html = html.replace('</body>', '<script src="/static/native-console.js"></script></body>')
                self.respond(200, html.encode(), 'text/html; charset=utf-8')
            elif path.startswith('/static/'):
                target = (STATIC / path.removeprefix('/static/')).resolve()
                media = {'.css': 'text/css', '.js': 'application/javascript', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf'}
                if not target.is_relative_to(STATIC) or not target.is_file() or target.suffix not in media:
                    return self.respond(404, {'error': '资源不存在'})
                self.respond(200, target.read_bytes(), media[target.suffix])
            elif path == '/core/status':
                self.respond(200, kernel.status_snapshot())
            elif path == '/core/syscalls':
                self.respond(200, {'syscalls': kernel.syscall_executor.get_history()})
            elif path == '/api/status':
                with console.lock:
                    jobs = [{key: job[key] for key in ('id', 'preset', 'policy', 'status')} for job in reversed(console.jobs.values())]
                self.respond(200, {'csrf': console.csrf, 'enforcement': 'cgroup_v2' if kernel.cgroup_manager.enabled else 'process_group', 'model': console.model_service.request('status')['model'],
                                  'kernel_status': kernel.status_snapshot(), 'machine': console.telemetry.sample(), 'syscalls': kernel.syscall_executor.get_history(), 'jobs': jobs})
            elif path == '/api/jobs':
                with console.lock:
                    jobs = [{key: job[key] for key in ('id', 'preset', 'policy', 'status')} for job in reversed(console.jobs.values())]
                self.respond(200, {'jobs': jobs})
            elif path.startswith('/api/jobs/'):
                try:
                    self.respond(200, console.detail(path.rsplit('/', 1)[1]))
                except KeyError:
                    self.respond(404, {'error': '运行不存在'})
            elif path == '/api/model-profiles':
                self.respond(200, console.model_service.request('profiles'))
            elif path == '/api/chat/status':
                self.respond(200, {'csrf': console.csrf, 'model': console.model_service.request('status')['model'],
                                  'knowledge': {'documents': len(console.knowledge.listing()['documents'])},
                                  'amem_count': len(kernel.memory_manager.list_notes(user_id='local'))})
            elif path == '/api/chat/conversations':
                self.respond(200, {'conversations': console.conversations.store.listing()})
            elif path.startswith('/api/chat/conversations/'):
                try:
                    self.respond(200, console.conversations.store.read(path.rsplit('/', 1)[1]))
                except (FileNotFoundError, ValueError):
                    self.respond(404, {'error': 'Conversation does not exist'})
            elif path == '/api/knowledge':
                self.respond(200, console.knowledge.listing())
            elif path == '/api/knowledge/search':
                self.respond(200, {'sources': console.knowledge.search(parse_qs(urlsplit(self.path).query).get('q', [''])[0])})
            elif path == '/api/aios/ecosystem':
                self.respond(200, {'amem_notes': kernel.memory_manager.list_notes(user_id='local'),
                                  'capabilities': {'plugins': [item for item in get_catalog()['plugins'] if item['id'] in BOUND_TOOLS or item['id'] in kernel.process_service.OPERATIONS or item['id'] in ('text.search', 'text.diff', 'json.validate', 'csv.profile', 'csv.aggregate', 'python.syntax', 'clock.now', 'library.search', 'library.read', 'tools.discover')], 'skills': get_catalog()['skills']},
                                  'agent_hub_apps': console.agent_hub.discover(), 'litecua_state': console.vm.observe(),
                                  'model_profiles': console.model_service.request('profiles')})
            else:
                self.respond(404, {'error': '此功能尚未接通'})

        def do_POST(self):
            if not self.local():
                return
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                return self.respond(403, {'error': '请求校验失败'})
            if self.path != '/query' and not compare_digest(self.headers.get('X-AgentRT-CSRF', ''), console.csrf):
                return self.respond(403, {'error': '请求校验失败'})
            if self.path == '/query' and origin and not compare_digest(self.headers.get('X-AgentRT-CSRF', ''), console.csrf):
                return self.respond(403, {'error': '请求校验失败'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= (12 * 1024 * 1024 if self.path == '/api/attachments' else 1024 * 1024):
                    raise ValueError('请求大小无效')
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict):
                    raise ValueError('需要 JSON 对象')
                if self.path == '/query':
                    return self.respond(200, console.kernel.handle_query_payload(value))
                if self.path == '/api/chat/turns':
                    if not value.pop('stream', False):
                        return self.respond(200, console.conversations.submit(value))
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.close_connection = True
                    def emit(kind, data):
                        try:
                            self.wfile.write(('event: ' + kind + '\ndata: ' + json.dumps(data, ensure_ascii=False) + '\n\n').encode())
                            self.wfile.flush()
                        except OSError:
                            pass
                    try:
                        result = console.conversations.submit(value, observer=lambda event: emit('delta', event))
                        emit('complete', result)
                    except Exception:
                        emit('failed', {'error': '请求未完成，未自动重试'})
                    return
                if self.path == '/api/aios/intent':
                    from aios.terminal.intent_router import IntentRouter
                    prompt = value.get('prompt')
                    if not isinstance(prompt, str) or not 0 < len(prompt) <= 2000:
                        raise ValueError('文件指令长度无效')
                    return self.respond(200, IntentRouter().execute_prompt(console.kernel, prompt, agent_name='console-storage'))
                if self.path == '/api/aios/litecua':
                    return self.respond(200, console.run_litecua(value))
                if self.path == '/api/attachments':
                    return self.respond(201, console.attachments.upload(value))
                if self.path in ('/api/model-profiles/save', '/api/model-profiles/select', '/api/model-profiles/remove'):
                    if not console.conversations.lock.acquire(blocking=False):
                        raise ValueError('请等待当前对话结束后再修改模型配置')
                    try:
                        return self.respond(200, console.model_service.request(self.path.rsplit('/', 1)[1], value))
                    finally:
                        console.conversations.lock.release()
                if self.path == '/api/jobs':
                    return self.respond(202, console.submit(value))
                parts = self.path.split('/')
                if len(parts) == 6 and parts[1:4] == ['api', 'aios', 'agents']:
                    name, operation = parts[4:]
                    drf = console.kernel.scheduler.drf_controller
                    if name not in drf.snapshot()['agents']:
                        return self.respond(404, {'error': 'Agent 不存在'})
                    if operation in ('pause', 'resume'):
                        state = drf.set_existing_agent_paused(name, operation == 'pause')
                    elif operation == 'terminate':
                        console.kernel.process_service.terminate(name)
                        state = drf.terminate_agent(name, ipc_manager=console.kernel.ipc_manager).state
                    else:
                        raise ValueError('控制操作无效')
                    return self.respond(200, {'state': state, 'in_flight_policy': 'drain_before_release'})
                if self.path == '/api/knowledge/save':
                    return self.respond(200, console.knowledge.save(value))
                if self.path == '/api/knowledge/remove':
                    return self.respond(200, console.knowledge.remove(value))
                if self.path == '/api/memory/save':
                    content = value.get('content')
                    if not isinstance(content, str) or not 0 < len(content) <= 4000:
                        raise ValueError('记忆最多 4000 字')
                    result = console.kernel.execute_request('console-memory', MemoryQuery(
                        operation_type='add_memory', params={'content': content, 'metadata': {'policy': 'shared'}}, user_id='local', agent_id='console-memory'))
                    return self.respond(200, result['response'].to_dict())
                self.respond(404, {'error': '此功能尚未接通'})
            except (ValueError, TypeError):
                self.respond(400, {'error': '输入无效或场景尚未接通'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18892)
    parser.add_argument('--data-dir', type=Path, default=Path('native-web-data'))
    parser.add_argument('--cgroup-root', help='Delegated subtree with cpu, memory and pids enabled')
    args = parser.parse_args()
    with AIOSKernelRuntime(storage_root=str(args.data_dir / 'storage'), cgroup_root=args.cgroup_root) as kernel:
        console = NativeConsole(kernel, args.data_dir / 'jobs')
        try:
            with create_server(console, args.port) as server:
                print(f'http://127.0.0.1:{server.server_port}/console/', flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
        finally:
            console.close()


if __name__ == '__main__':
    main()
