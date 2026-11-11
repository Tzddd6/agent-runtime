"""单控制进程的初版 Runtime。任务状态和资源预留只在此修改。"""
import json
import math
import queue
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from .context import ContextStore
from .executor import Executor
from .model_gateway import ModelGateway, ModelError
from .ipc import Broker
from .telemetry import CgroupSampler
from .scheduling import AdmissionScheduler


TERMINAL_BAD = {'failed', 'timeout', 'skipped', 'unschedulable', 'cancelled'}
DEFAULTS = {'cpu_milli': 1000, 'memory_bytes': 256 * 1024**2, 'pids': 32}


class _ObserverChannel:
    """Bounded, lossy telemetry delivery; it never gates Worker scheduling.

    One daemon consumer per workflow, at most eight pending snapshots, and a
    250ms final flush. A callback that never returns can outlive ``run``; Python
    threads do not provide OS isolation or forcible termination of hostile code.
    Callbacks must be trusted, and may miss intermediate snapshots under load.
    """
    def __init__(self, callback):
        self.callback = callback
        self.pending = queue.Queue(maxsize=8)
        self.closing = threading.Event()
        self.abandon = threading.Event()
        self.thread = threading.Thread(target=self._consume, name='agentrt-observer', daemon=True)
        self.thread.start()

    def publish(self, sample):
        if self.closing.is_set():
            return
        try:
            self.pending.put_nowait(sample)
        except queue.Full:
            try:
                self.pending.get_nowait()
                self.pending.task_done()
            except queue.Empty:
                pass
            try:self.pending.put_nowait(sample)
            except queue.Full:pass

    def _consume(self):
        while not self.abandon.is_set():
            try:sample = self.pending.get(timeout=.025)
            except queue.Empty:
                if self.closing.is_set():return
                continue
            try:
                self.callback(sample)
            except BaseException:
                # Callback errors (including SystemExit in a plugin) are local
                # to telemetry; they cannot change Task/Attempt state.
                pass
            finally:
                self.pending.task_done()

    def close(self, timeout=.25):
        self.closing.set()
        self.thread.join(timeout)
        if self.thread.is_alive():
            self.abandon.set()
            # Release queued snapshots even if the current callback is stuck.
            while True:
                try:self.pending.get_nowait()
                except queue.Empty:break
                else:self.pending.task_done()


def validate(tasks):
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('任务必须为非空数组')
    pending = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError('每个任务必须为 JSON 对象')
        key = task.get('id')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key) or key in pending:
            raise ValueError('任务 ID 必须唯一，且只包含字母、数字、下划线和连字符')
        deps = task.get('depends_on', [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ValueError('depends_on 必须为字符串数组')
        pending[key] = set(deps)
        for field in ('retries', 'fail_attempts'):
            value = task.get(field, 0)
            if type(value) is not int or not 0 <= value <= 3:
                raise ValueError(f'{field} 必须为 0—3 的整数')
        if not isinstance(task.get('agent', key), str) or not isinstance(task.get('contexts', []), list):
            raise ValueError('agent 必须为字符串，contexts 必须为数组')
        if not isinstance(task.get('resources', {}), dict):
            raise ValueError('resources 必须为对象')
        inputs = task.get('inputs', [])
        if not isinstance(inputs, list) or not all(isinstance(i, str) and i in deps for i in inputs):
            raise ValueError('inputs 必须为直接依赖任务 ID 的数组')
        for name, value in task.get('resources', {}).items():
            if name not in DEFAULTS or type(value) is not int or value <= 0:
                raise ValueError('资源请求必须为已知字段的正整数')
        duration = task.get('timeout', 10)
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise ValueError('任务期限必须为有限正数')
    if any(deps - pending.keys() for deps in pending.values()):
        raise ValueError('存在未知依赖')
    done = set()
    while pending:
        ready = [key for key, deps in pending.items() if deps <= done]
        if not ready:
            raise ValueError('任务依赖存在环')
        for key in ready:
            done.add(key)
            del pending[key]


def run(tasks, max_workers=2, timeout=10, *, output_dir=None, budget=None,
        policy='resource_fit', cgroup_root=None, max_tasks=100, cancel_event=None, quiet=False, observer=None):
    validate(tasks)
    if (type(max_workers) is not int or max_workers < 1 or isinstance(timeout,bool)
            or not isinstance(timeout,(int,float)) or timeout <= 0 or not math.isfinite(timeout)
            or policy not in AdmissionScheduler.POLICIES
            or type(max_tasks) is not int or max_tasks < 1 or (observer is not None and not callable(observer))):
        raise ValueError('无效的并发数、期限或调度策略')
    if len(tasks) > max_tasks:
        raise ValueError('任务数超过上限')
    budget = dict({'cpu_milli': max_workers * 1000, 'memory_bytes': 1024**3, 'pids': 128}
                  if budget is None else budget)
    if set(budget) != set(DEFAULTS) or any(type(v) is not int or v <= 0 for v in budget.values()):
        raise ValueError('预算必须包含全部资源字段及正整数值')
    scheduler = AdmissionScheduler(policy,budget,DEFAULTS)
    executor = Executor(cgroup_root)
    root = Path(output_dir or tempfile.mkdtemp(prefix='agentrt-')).resolve() / uuid.uuid4().hex
    root.mkdir(parents=True)
    store = ContextStore(root / 'contexts')
    tasks = [dict(t) for t in tasks]
    task_by_id = {t['id']: t for t in tasks}
    states = {t['id']: 'waiting' for t in tasks}
    attempts = {t['id']: 0 for t in tasks}
    used = {k: 0 for k in budget}
    running, events = {}, []
    gateway = broker = None
    outputs = {}
    sampler, last_telemetry, terminal_usage = CgroupSampler(), [0], {}
    journal = (root / 'events.jsonl').open('w', encoding='utf-8')
    channel = _ObserverChannel(observer) if observer is not None else None
    sample_sequence = [0]

    def observe(force=False):
        if observer is None or (not force and time.monotonic()-last_telemetry[0] < .4):
            return
        last_telemetry[0] = time.monotonic()
        rows = []
        for t in tasks:
            key, agent = t['id'], t.get('agent', t['id'])
            limits = {**DEFAULTS, **t.get('resources', {})}
            handle = running.get(key, (None,None,None))[0]
            reason = ''
            if states[key] == 'waiting':
                if not all(states[d]=='succeeded' for d in t.get('depends_on',[])):
                    reason = '等待依赖任务'
                elif any(other.get('agent',other['id'])==agent for _,other,_ in running.values()):
                    reason = '同一 Agent 正在执行'
                elif scheduler.last_decision.draining_task_id:
                    reason = '排空资源，优先让 '+scheduler.last_decision.draining_task_id+' 启动'
                elif len(running)>=max_workers:
                    reason = '等待 Worker 槽位'
                elif any(used[k]+limits[k]>budget[k] for k in budget):
                    reason = '等待资源预算释放'
                else:
                    reason = '等待调度' if policy!='fifo' else '等待 FIFO 队列调度'
            metrics = sampler.sample(handle['group']) if handle else dict(terminal_usage.get(key,{}))
            rows.append({'id':key,'agent':agent,'state':states[key],'attempt':attempts[key],
                         'depends_on':list(t.get('depends_on',[])),'resources':limits,'wait_reason':reason,
                         'pid':handle['proc'].pid if handle else None,'metrics':metrics})
        sample_sequence[0] += 1
        sample = {'workflow_id':root.name,'time':time.time(),'tasks':rows,
                  'sample_sequence':sample_sequence[0],'event_sequence':len(events),
                  'budget':dict(budget),'reserved':dict(used),'slots_used':len(running),
                  'slots_total':max_workers,'policy':policy,
                  'context':store.stats(),
                  'scheduling':{'reason':scheduler.last_decision.reason,
                    'draining_task_id':scheduler.last_decision.draining_task_id,
                    'waited_seconds':scheduler.last_decision.waited_seconds},
                  'enforcement':'cgroup_v2' if cgroup_root else 'admission_only'}
        channel.publish(sample)

    def event(key, state, **extra):
        states[key] = state
        item = {'workflow_id': root.name, 'task_id': key, 'state': state,
                'agent': task_by_id[key].get('agent', key),
                'time': time.time(), 'attempt': attempts[key], 'sequence':len(events)+1, **extra}
        events.append(item)
        journal.write(json.dumps(item, ensure_ascii=False) + '\n')
        journal.flush()
        if not quiet:
            print(json.dumps(item, ensure_ascii=False), flush=True)
        observe(force=True)

    def cleanup_attempt(key, handle, limits):
        try:
            result = executor.cleanup(handle)
        except Exception as exc:
            raise RuntimeError(f'CLEANUP_FAILED: {key}: {exc}') from exc
        # A successful cleanup is the sole authority for releasing a reservation.
        for name in used:
            used[name] -= limits[name]
        del running[key]
        sampler.forget(handle['group'])
        if broker:
            broker.revoke(handle.get('ipc_token'))
        return result

    try:
        observe(force=True)
        while any(s in ('waiting', 'running') for s in states.values()):
            if cancel_event is not None and cancel_event.is_set():
                for key, (handle, task, limits) in list(running.items()):
                    cleanup_attempt(key,handle,limits)
                    event(key, 'cancelled', reason='用户取消；远端模型是否停止及计费状态未知')
                for key in states:
                    if states[key] == 'waiting':
                        event(key, 'cancelled', reason='用户取消')
                break
            def choose():
                ready = [t for t in tasks if states[t['id']]=='waiting'
                         and all(states[d]=='succeeded' for d in t.get('depends_on',[]))]
                return scheduler.choose(ready,used,
                    busy_agents={t.get('agent',t['id']) for _,t,_ in running.values()},
                    slots_available=max_workers-len(running))
            choose()
            for task in list(tasks):
                key = task['id']
                if states[key] != 'waiting':
                    continue
                deps = task.get('depends_on', [])
                if any(states[d] in TERMINAL_BAD for d in deps):
                    event(key, 'skipped', reason='依赖未成功')
                    continue
                if not all(states[d] == 'succeeded' for d in deps):
                    continue
                limits = {**DEFAULTS, **task.get('resources', {})}
                if any(limits[k] > budget[k] for k in budget):
                    event(key, 'unschedulable', reason='单任务请求超过总预算')
                    continue
                agent = task.get('agent', key)
                if choose().task_id!=key:
                    continue
                busy = any(t.get('agent', t['id']) == agent for _, t, _ in running.values())
                if busy or len(running) >= max_workers or any(used[k] + limits[k] > budget[k] for k in budget):
                    if policy == 'fifo':
                        break
                    continue
                attempts[key] += 1
                payload = {**task, '_attempt': attempts[key], 'timeout':task.get('timeout',timeout)}
                try:
                    refs = [store.put(data, [agent]) for data in task.get('contexts', [])]
                    payload['_contexts'] = [store.get(ref, agent) for ref in refs]
                    payload['_inputs'] = {name: outputs[name] for name in task.get('inputs', [])}
                    # Catch non-JSON Python API configuration before OS launch.
                    json.dumps(payload,ensure_ascii=False)
                except (ValueError,TypeError,KeyError,PermissionError,FileNotFoundError) as exc:
                    event(key,'failed',error=f'任务输入准备失败: {exc}')
                    continue
                if task.get('action') in ('send', 'receive'):
                    if broker is None:
                        broker = Broker(store, [t.get('agent', t['id']) for t in tasks])
                    payload['_ipc_url'] = broker.url
                    payload['_ipc_token'] = broker.issue(agent)
                if task.get('action') == 'llm':
                    try:
                        if gateway is None:
                            gateway = ModelGateway.from_env(root / 'model-calls.jsonl')
                        payload['_gateway_url'] = gateway.url
                        payload['_gateway_token'] = gateway.token
                    except ModelError as exc:
                        event(key, 'failed', error=str(exc))
                        continue
                directory = root / f'{key}-{attempts[key]}'
                try:
                    handle = executor.launch(payload, directory, limits)
                except (ValueError,TypeError) as exc:
                    if broker:broker.revoke(payload.get('_ipc_token'))
                    event(key,'failed',error=f'任务启动载荷无效: {exc}')
                    continue
                except OSError as exc:
                    if broker:
                        broker.revoke(payload.get('_ipc_token'))
                    if cgroup_root:
                        raise RuntimeError(f'EXECUTION_BACKEND_FAILED: Worker 委派或启动失败: {exc}') from exc
                    event(key, 'failed', error=f'启动失败: {exc}')
                    continue
                handle['ipc_token'] = payload.get('_ipc_token')
                for k in used:
                    used[k] += limits[k]
                running[key] = (handle, task, limits)
                scheduler.started(key)
                event(key, 'running', pid=handle['proc'].pid, resources=limits,
                      enforcement='cgroup_v2' if cgroup_root else 'admission_only', context_refs=refs)
            for key, (handle, task, limits) in list(running.items()):
                proc = handle['proc']
                expired_flag = handle.get('expired')
                expired = ((expired_flag is not None and expired_flag.is_set()) or
                           (proc.poll() is None and time.monotonic()-handle['started'] >= task.get('timeout',timeout)))
                if proc.poll() is None and not expired:
                    continue
                code, diagnostic, usage = cleanup_attempt(key,handle,limits)
                expired = expired or (expired_flag is not None and expired_flag.is_set())
                cpu = dict(line.split() for line in usage.get('cpu.stat','').splitlines())
                terminal_usage[key] = {'source':'cgroup_v2' if cgroup_root else 'unavailable',
                    'cpu_cores':None,'memory_bytes':None,'pids':None,
                    'cpu_total_seconds':int(cpu['usage_usec'])/1e6 if 'usage_usec' in cpu else None,
                    'memory_peak_bytes':int(usage['memory.peak']) if 'memory.peak' in usage else None,
                    'throttled_periods':int(cpu.get('nr_throttled',0)) if cpu else None}
                result, error = {}, None
                oom = re.search(r'^oom_kill\s+([0-9]+)', usage.get('memory.events', ''), re.M)
                if oom and int(oom.group(1)):
                    error = 'OOM: cgroup 内存超限，不按原预算盲目重试'
                result_file = handle['directory'] / 'result.json'
                if code == 0 and not expired:
                    try:
                        if result_file.stat().st_size > 1024 * 1024:
                            raise ValueError('结果超过 1 MiB')
                        result = json.loads(result_file.read_text(encoding='utf-8'))
                        if not isinstance(result, dict) or result.get('task_id') != key:
                            raise ValueError('结果 task_id 不匹配')
                        children = result.get('spawn', [])
                        if not isinstance(children, list) or len(tasks) + len(children) > max_tasks:
                            raise ValueError('动态任务批次超过限制')
                        if children:
                            # 在更新任何状态前验证整批，失败不留下半张图。
                            validate(tasks + children)
                            tasks.extend(children)
                            for child in children:
                                states[child['id']] = 'waiting'
                                attempts[child['id']] = 0
                                task_by_id[child['id']] = child
                            if broker:
                                with broker.lock:
                                    broker.agents.update(t.get('agent', t['id']) for t in children)
                    except (ValueError, OSError, TypeError, KeyError) as exc:
                        error = f'协议错误: {exc}'
                state = 'timeout' if expired else ('succeeded' if code == 0 and error is None else 'failed')
                if (task.get('action') != 'llm' and state in ('failed', 'timeout') and not error
                        and attempts[key] <= task.get('retries', 0)):
                    event(key, 'waiting', reason='清理完成，创建新 Attempt 重试', exit_code=code, usage=usage)
                else:
                    if state == 'succeeded':
                        outputs[key] = result.get('result')
                    event(key, state, exit_code=code, output=diagnostic, result=result,
                          error=error, usage=usage)
            if running:
                observe()
                time.sleep(0.02)
    finally:
        cleanup_errors = []
        teardown_errors = []
        for key,(handle,_,limits) in list(running.items()):
            try:
                cleanup_attempt(key,handle,limits)
            except Exception as exc:
                cleanup_errors.append(str(exc))
            else:
                if states[key]=='running':states[key]='failed'
        try:
            observe(force=True)
        except Exception as exc:
            teardown_errors.append(f'telemetry final snapshot: {exc}')
        finally:
            # Attempt each independent closer even when an earlier one fails.
            for name,closer in [('model gateway',gateway.close if gateway else None),
                                ('IPC broker',broker.close if broker else None),
                                ('observer',channel.close if channel else None),('journal',journal.close)]:
                if closer is not None:
                    try:closer()
                    except Exception as exc:teardown_errors.append(f'{name}: {exc}')
        if cleanup_errors:
            raise RuntimeError('CLEANUP_FAILED: '+'; '.join(cleanup_errors))
        if teardown_errors:
            raise RuntimeError('TEARDOWN_FAILED: '+'; '.join(teardown_errors))
    (root / 'summary.json').write_text(json.dumps({'states': states, 'attempts': attempts,
                                                'context_stats': store.stats()},
                                                ensure_ascii=False, indent=2), encoding='utf-8')
    return states, events
