"""Loopback console backed by the native AIOS workflow engine."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
from threading import Lock
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from aios.os_kernel.telemetry import MachineSampler
from aios.scheduler.workflow_dispatch import WorkflowDispatchQueue
from aios.storage.database import RuntimeDatabase
from aios.storage.knowledge import KnowledgeStore
from aios.storage.attachments import AttachmentStore
from aios.llm_core.model_service import ModelService, ModelServiceUnavailable
from aios.llm_core.diagnostics import MESSAGES
from runtime.conversation import Conversations
from runtime.engineering_recovery import ReconciliationRequired, interrupted_worker
from aios.tool.catalog import get_catalog
from aios.tool.bindings import BOUND_TOOLS
from aios.tool.virtual_env.desktop_env import get_vm_controller
from cerebrum.hub import AgentHub, AgentManifest
from cerebrum.memory.apis import MemoryQuery
from cerebrum.tool.apis import ToolQuery
from runtime.launch import AIOSKernelRuntime

STATIC = Path(__file__).with_name('static').resolve()
CONCURRENT_PRESETS = ('document', 'resources')
JOB_HISTORY_LIMIT = 20
LOG = logging.getLogger(__name__)


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


class ModelNotConfigured(ValueError):
    pass


class JobBusy(ValueError):
    pass


class NativeConsole:
    def __init__(self, kernel, root):
        self.kernel, self.root = kernel, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.csrf = secrets.token_urlsafe(32)
        self.lock = Lock()
        self.jobs = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='aios-web')
        self.workflow_dispatch = WorkflowDispatchQueue(kernel.scheduler.drf_controller, 'critical_path')
        self.database = RuntimeDatabase(self.root / 'database')
        self.knowledge = KnowledgeStore(self.database)
        self.attachments = AttachmentStore(self.root / 'attachments', self.database)
        self.model_service = ModelService()
        self.kernel.llm.model_service = self.model_service
        self.telemetry = MachineSampler(kernel.cgroup_manager.cgroup_root if kernel.cgroup_manager.enabled else None)
        self.vm = get_vm_controller()
        self.agent_hub = _default_agent_hub()
        self.conversations = Conversations(self)
        for path in sorted(self.root.glob('*.json'), key=lambda path: path.stat().st_mtime):
            if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                continue
            try:
                job = json.loads(path.read_text(encoding='utf-8'))
                if job['id'] != path.stem or len(path.stem) > 96 or not path.stem.isascii() or not path.stem.isalnum():
                    continue
                if job['status'] == 'running':
                    try:
                        saved = kernel.workflow_engine.load_checkpoint(job['id'])
                    except (ValueError, OSError, sqlite3.Error):
                        saved = None
                    restartable = bool(job.get('resume_request') and interrupted_worker(saved, job['id']))
                    job['recovery_available'] = restartable
                    uncertain = bool(saved and any(
                        state in ('running', 'needs_reconciliation') for state in saved['states'].values()
                    ))
                    job['status'] = 'needs_reconciliation' if uncertain else 'interrupted'
                    job['error'] = ('验证 Worker 已终止并回收，原补丁可用于恢复。' if restartable
                                    else '运行在中断时的结果未确认，请核对后处理。')
                    if saved:
                        job.update(states=saved['states'], events=saved['events'],
                                   context_refs=saved['context_refs'])
                    self.save(job)
                self.jobs[job['id']] = job
            except (OSError, ValueError, KeyError, TypeError):
                continue
        self.jobs = dict(sorted(self.jobs.items(), key=lambda item: item[1].get('created', 0)))
        self.prune_jobs()

    def prune_jobs(self):
        """Called under the console lock after accepting, completing or unpinning a job."""
        excess = sum(not job.get('pinned') and not job.get('retiring') for job in self.jobs.values()) - JOB_HISTORY_LIMIT
        for identity, job in list(self.jobs.items()):
            if job.get('pinned') or job['status'] == 'running':
                continue
            if not job.get('retiring') and excess <= 0:
                continue
            try:
                if not job.get('retiring'):
                    self.save({**job, 'retiring': True})
                    job['retiring'] = True
                    excess -= 1
                self.kernel.workflow_engine.purge_checkpoint(identity)
                agent_ids = {
                    name for name in [*job.get('owned_agents', []),
                                      *(node.get('agent') for node in job.get('graph', []))]
                    if isinstance(name, str) and name.startswith(identity + '-')
                }
                ctx = self.kernel.context_store
                ctx.release_agent(f'workflow:{identity}', strict=True, revoke_agents=agent_ids)
                for name in agent_ids:
                    ctx.release_agent(name, strict=True)
                    self.kernel.scheduler.drf_controller.discard_idle_agent(name)
                (self.root / f'{identity}_delivery.zip').unlink(missing_ok=True)
                (self.root / f'{identity}.json').unlink(missing_ok=True)
            except (OSError, RuntimeError, sqlite3.Error):
                LOG.exception('Could not finish retiring workflow %s', identity)
            else:
                del self.jobs[identity]

    def pin_job(self, identity, pinned):
        if type(pinned) is not bool:
            raise ValueError('保留标记需要是布尔值')
        with self.lock:
            job = self.jobs.get(identity)
            if job is None or job.get('retiring'):
                raise KeyError(identity)
            if job['status'] == 'running':
                raise JobBusy('任务结束后可以修改保留标记')
            self.save({**job, 'pinned': pinned})
            job['pinned'] = pinned
            self.prune_jobs()
        return {'id': identity, 'pinned': pinned}

    def model_status(self):
        try:
            return self.model_service.request('status')
        except (TimeoutError, ModelServiceUnavailable):
            return {'configured': False, 'model': '模型服务不可用'}

    def close(self):
        self.pool.shutdown(wait=True)
        self.model_service.close()

    def detail(self, identity):
        with self.lock:
            job = deepcopy(self.jobs[identity])
            if job.get('retiring'):
                raise KeyError(identity)
            job.pop('resume_request', None)
        graph = next((graph for graph in self.kernel.workflow_engine.recent_graphs() if graph['id'] == identity), None)
        if graph:
            job['graph'] = graph['nodes']
            if job['status'] == 'running':
                job['states'] = {node['id']: node['state'] for node in graph['nodes']}
                job['attempts'] = {node['id']: node['attempts'] for node in graph['nodes']}
        return job

    def download_zip(self, identity):
        if not isinstance(identity, str) or not identity.isalnum():
            raise ValueError('无效的任务 ID')
        with self.lock:
            job = deepcopy(self.jobs[identity])
            if job.get('retiring'):
                raise KeyError(identity)
            if job.get('status') not in ('succeeded', 'completed_with_failures'):
                raise ValueError('仅允许下载已完成且通过主链验收的交付包')
            eng = job.get('engineering') or {}
            artifact = eng.get('artifact') or {}
            expected_zip_sha = artifact.get('zip_sha256')
            expected_src_sha = eng.get('source_sha256') or artifact.get('sha256')
            if not eng.get('accepted') or not expected_zip_sha or not expected_src_sha:
                raise ValueError('该任务未生成已验证的交付产物')
            zip_path = self.root / f'{identity}_delivery.zip'
            if zip_path.is_symlink() or not zip_path.is_file():
                raise FileNotFoundError(identity)
            raw = zip_path.read_bytes()
        from hashlib import sha256
        if sha256(raw).hexdigest() != expected_zip_sha:
            raise ValueError('交付压缩包 SHA-256 校验失败，文件可能已损坏或被篡改')
        from aios.tool.engineering import verify_delivery_zip_bytes
        verified = verify_delivery_zip_bytes(raw, expected_src_sha, multi_file=bool(eng.get('multi_file', False)))
        if eng.get('execution_evidence') and eng['execution_evidence'] != verified.get('execution_evidence'):
            raise ValueError('交付压缩包的恢复凭据与任务记录不一致')
        return raw

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
        if preset not in ('dynamic', 'document', 'kernel', 'resources', 'agents', 'ipc', 'litecua', 'llm', 'team', 'engineering', 'scheduling', 'workflow_scheduling'):
            raise ValueError('不支持的任务场景')
        workers = value.get('workers', 2)
        if type(workers) is not int or not 1 <= workers <= 4:
            raise ValueError('并发数需为 1 至 4')
        text = value.get('prompt', '')
        if not isinstance(text, str) or len(text) > 4000:
            raise ValueError('文本最多 4000 字')
        deadline_ms = value.get('deadline_ms')
        if deadline_ms is not None and (preset not in CONCURRENT_PRESETS or type(deadline_ms) is not int
                                        or not 50 <= deadline_ms <= 300_000):
            raise ValueError('离线任务的截止时间须在 50 到 300000 毫秒之间')
        with self.lock:
            active = [job for job in self.jobs.values() if job['status'] == 'running']
            if active and (len(active) >= 2 or preset not in CONCURRENT_PRESETS
                           or any(job['preset'] not in CONCURRENT_PRESETS for job in active)):
                raise JobBusy('资源压力和离线文本统计最多可同时运行两项，请等待当前任务结束')
            needs_model = preset in ('llm', 'team', 'engineering')
            if needs_model and not value.get('deterministic_stub'):
                if not self.model_service.request('status')['configured']:
                    raise ModelNotConfigured('请先保存并选用模型配置')
            identity = uuid4().hex
            policy = {'scheduling': 'fifo_vs_drf', 'workflow_scheduling': 'cross_workflow_comparison'}
            job_policy = 'critical_path' if preset in CONCURRENT_PRESETS else policy.get(preset, 'native_drf')
            deadline_at = time.monotonic() + deadline_ms / 1000 if deadline_ms is not None else None
            job = {'id': identity, 'preset': preset, 'policy': job_policy, 'status': 'running',
                   'created': time.time(), 'states': {}, 'attempts': {}, 'events': []}
            if deadline_ms is not None:
                job['deadline_ms'] = deadline_ms
            if needs_model:
                payload = self.conversation_payload(identity, value)
                if preset == 'engineering' and payload['interrupt_verification'] is True:
                    job['resume_request'] = payload
            self.jobs[identity] = job
            try:
                self.save(job)
                if needs_model:
                    self.pool.submit(self.execute_conversation, identity, payload)
                else:
                    self.pool.submit(self.execute, identity, preset, text, workers, deadline_at)
            except Exception:
                self.jobs.pop(identity)
                (self.root / (identity + '.json')).unlink(missing_ok=True)
                raise
            self.prune_jobs()
        return {'id': identity}

    def save(self, job):
        path = self.root / (job['id'] + '.json')
        temporary = path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(job, stream, ensure_ascii=False, separators=(',', ':'), default=lambda o: '<callable>' if callable(o) else str(o))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    @staticmethod
    def conversation_payload(identity, value):
        return {
            'conversation_id': 'job_' + identity,
            'request_id': identity,
            'mode': value['preset'],
            'project': value.get('project', 'pricing'),
            'prompt': value.get('prompt') or '完成当前受控任务',
            'allow_repair_round_2': value.get('allow_repair_round_2', True),
            'include_audit_branch': value.get('include_audit_branch', False),
            'simulate_audit_failure': value.get('simulate_audit_failure', False),
            'deterministic_stub': value.get('deterministic_stub', False),
            'interrupt_verification': value.get('interrupt_verification', False),
            'model_budget': value.get('model_budget'),
        }

    def resume_job(self, identity):
        with self.lock:
            job = self.jobs.get(identity)
            if job is None or job.get('retiring'):
                raise KeyError(identity)
            if any(entry['status'] == 'running' for entry in self.jobs.values()):
                raise JobBusy('请等待当前任务结束')
            if job['status'] != 'interrupted' or not job.get('recovery_available') or not job.get('resume_request'):
                raise ReconciliationRequired('该任务没有已确认回收的测试 Worker 可供恢复。')
            prior = dict(job)
            job.update(status='running', recovery_available=False)
            try:
                self.save(job)
                self.pool.submit(self.execute_conversation, identity, job['resume_request'], resume=True)
            except Exception:
                self.jobs[identity] = prior
                self.save(prior)
                raise
        return {'id': identity}

    def execute_conversation(self, identity, payload, *, resume=False):
        try:
            self.conversations.submit(payload, workflow_id=identity, resume=resume)
        except Exception:
            with self.lock:
                job = self.jobs.get(identity)
                if job and job['status'] == 'running':
                    job.update(status='failed', error='执行未完成，未自动重提模型请求', finished=time.time())
                    self.save(job)
                    self.prune_jobs()

    def execute(self, identity, preset, text, workers, deadline_at=None):
        try:
            if preset == 'scheduling':
                from runtime.scheduling_comparison import compare_scheduling
                comparison = compare_scheduling()
                result = {'scheduling': comparison}
                status = 'succeeded' if comparison['verified'] else 'completed_with_failures'
            elif preset == 'workflow_scheduling':
                from runtime.workflow_scheduling_benchmark import compare_workflows
                comparison = compare_workflows()
                result = {'workflow_scheduling': comparison}
                status = 'succeeded' if comparison['verified'] else 'completed_with_failures'
            else:
                result = self.run_scenario(identity, preset, text, workers, deadline_at)
                status = 'completed_with_failures' if any(
                    state in ('failed', 'unschedulable') for state in result['states'].values()
                ) else 'succeeded'
        except Exception:
            result, status = {'error': '任务未完成，请核对内核记录'}, 'failed'
        if deadline_at is not None:
            result['deadline_met'] = status == 'succeeded' and time.monotonic() <= deadline_at
        with self.lock:
            self.jobs[identity].update(result, status=status, finished=time.time())
            self.save(self.jobs[identity])
            self.prune_jobs()

    def run_scenario(self, identity, preset, text, workers, deadline_at=None):
        def summary(task, upstream, attempt):
            return {'summary': upstream}

        if preset in ('kernel', 'resources', 'agents', 'ipc', 'litecua'):
            from runtime.scenarios import tasks as native_tasks
            tasks = native_tasks(self.kernel, preset, identity, text)
        elif preset == 'document':
            tasks = [{'id': 'stats', 'agent': identity + '-stats', 'estimate_seconds': .080,
                      'query_type': 'tool', 'query_data': {'tool_calls': [
                {'name': 'text.stats', 'parameters': {'text': text}}]}},
                {'id': 'report', 'agent': identity + '-report', 'depends_on': ['stats'],
                 'estimate_seconds': .005, 'handler': summary}]
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
        return self.kernel.workflow_engine.run_workflow(
            identity, tasks, max_workers=workers, context_holder=f'workflow:{identity}',
            dispatch_queue=self.workflow_dispatch if preset in CONCURRENT_PRESETS else None,
            deadline_at=deadline_at,
        )


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
            if path == '/favicon.ico':
                self.send_response(204)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if path in ('/', '/console', '/console/'):
                document = STATIC / ('chat.html' if path == '/' else 'console.html')
                return self.respond(200, document.read_bytes(), 'text/html; charset=utf-8')
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
                    jobs = [{key: job[key] for key in ('id', 'preset', 'policy', 'status')} for job in reversed(console.jobs.values()) if not job.get('retiring')]
                self.respond(200, {'csrf': console.csrf, 'enforcement': 'cgroup_v2' if kernel.cgroup_manager.enabled else 'process_group', 'model': console.model_status()['model'],
                                  'kernel_status': kernel.status_snapshot(), 'machine': console.telemetry.sample(), 'syscalls': kernel.syscall_executor.get_history(), 'jobs': jobs})
            elif path == '/api/jobs':
                with console.lock:
                    jobs = [{key: job[key] for key in ('id', 'preset', 'policy', 'status')} for job in reversed(console.jobs.values()) if not job.get('retiring')]
                self.respond(200, {'jobs': jobs})
            elif path.startswith('/api/jobs/'):
                parts = path.strip('/').split('/')
                if len(parts) == 4 and parts[3] == 'download':
                    try:
                        raw_zip = console.download_zip(parts[2])
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/zip')
                        self.send_header('Content-Disposition', f'attachment; filename="aios_delivery_{parts[2][:10]}.zip"')
                        self.send_header('Content-Length', str(len(raw_zip)))
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        self.wfile.write(raw_zip)
                        return
                    except (KeyError, FileNotFoundError):
                        return self.respond(404, {'error': '交付压缩包不存在'})
                    except ValueError as exc:
                        return self.respond(409, {'error': str(exc)})
                try:
                    self.respond(200, console.detail(path.rsplit('/', 1)[1]))
                except KeyError:
                    self.respond(404, {'error': '运行不存在'})
            elif path == '/api/model-profiles':
                self.respond(200, console.model_service.request('profiles'))
            elif path == '/api/chat/status':
                model = console.model_status()
                self.respond(200, {'csrf': console.csrf, 'model': model['model'], 'model_configured': model['configured'],
                                  'model_profile': model,
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
                if self.path == '/api/model-profiles/test':
                    return self.respond(200, console.model_service.request('test_connection', value))
                if self.path in ('/api/model-profiles/save', '/api/model-profiles/save-and-select',
                                 '/api/model-profiles/select', '/api/model-profiles/remove'):
                    if not console.conversations.lock.acquire(blocking=False):
                        raise ValueError('请等待当前对话结束后再修改模型配置')
                    try:
                        return self.respond(200, console.model_service.request(self.path.rsplit('/', 1)[1], value))
                    finally:
                        console.conversations.lock.release()
                if self.path == '/api/jobs':
                    return self.respond(202, console.submit(value))
                if self.path.startswith('/api/jobs/'):
                    parts = self.path.split('/')
                    if len(parts) == 5:
                        try:
                            if parts[4] == 'retention':
                                return self.respond(200, console.pin_job(parts[3], value.get('pinned')))
                            if parts[4] == 'resume':
                                return self.respond(202, console.resume_job(parts[3]))
                        except KeyError:
                            return self.respond(404, {'error': '运行不存在'})
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
            except TimeoutError:
                self.respond(504, {'error': MESSAGES['request_timeout']})
            except ModelServiceUnavailable:
                self.respond(503, {'error': MESSAGES['service_unavailable']})
            except (JobBusy, ReconciliationRequired) as exc:
                self.respond(409, {'error': str(exc)})
            except ModelNotConfigured as exc:
                self.respond(400, {'error': str(exc)})
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
