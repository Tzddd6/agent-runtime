"""单控制进程的初版 Runtime。任务状态和资源预留只在此修改。"""
import json
import math
import re
import tempfile
import time
import uuid
from pathlib import Path
from .context import ContextStore
from .executor import Executor


TERMINAL_BAD = {'failed', 'timeout', 'skipped', 'unschedulable'}
DEFAULTS = {'cpu_milli': 1000, 'memory_bytes': 256 * 1024**2, 'pids': 32}


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
        policy='resource_fit', cgroup_root=None, max_tasks=100):
    validate(tasks)
    if max_workers < 1 or timeout <= 0 or not math.isfinite(timeout) or policy not in ('fifo', 'resource_fit'):
        raise ValueError('无效的并发数、期限或调度策略')
    if len(tasks) > max_tasks:
        raise ValueError('任务数超过上限')
    budget = dict(budget or {'cpu_milli': max_workers * 1000, 'memory_bytes': 1024**3, 'pids': 128})
    if set(budget) != set(DEFAULTS) or any(type(v) is not int or v <= 0 for v in budget.values()):
        raise ValueError('预算必须包含全部资源字段及正整数值')
    executor = Executor(cgroup_root)
    root = Path(output_dir or tempfile.mkdtemp(prefix='agentrt-')).resolve() / uuid.uuid4().hex
    root.mkdir(parents=True)
    store = ContextStore(root / 'contexts')
    tasks = [dict(t) for t in tasks]
    states = {t['id']: 'waiting' for t in tasks}
    attempts = {t['id']: 0 for t in tasks}
    used = {k: 0 for k in budget}
    running, events = {}, []
    journal = (root / 'events.jsonl').open('w', encoding='utf-8')

    def event(key, state, **extra):
        states[key] = state
        item = {'workflow_id': root.name, 'task_id': key, 'state': state,
                'time': time.time(), 'attempt': attempts[key], **extra}
        events.append(item)
        journal.write(json.dumps(item, ensure_ascii=False) + '\n')
        journal.flush()
        print(json.dumps(item, ensure_ascii=False), flush=True)

    try:
        while any(s in ('waiting', 'running') for s in states.values()):
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
                busy = any(t.get('agent', t['id']) == agent for _, t, _ in running.values())
                if busy or len(running) >= max_workers or any(used[k] + limits[k] > budget[k] for k in budget):
                    if policy == 'fifo':
                        break
                    continue
                attempts[key] += 1
                payload = {**task, '_attempt': attempts[key]}
                # 初版用受控任务配置指定上下文；真实 IPC 调用授权留待后续。
                refs = [store.put(data, [agent]) for data in task.get('contexts', [])]
                payload['_contexts'] = [store.get(ref, agent) for ref in refs]
                directory = root / f'{key}-{attempts[key]}'
                try:
                    handle = executor.launch(payload, directory, limits)
                except OSError as exc:
                    event(key, 'failed', error=f'启动失败: {exc}')
                    continue
                for k in used:
                    used[k] += limits[k]
                running[key] = (handle, task, limits)
                event(key, 'running', pid=handle['proc'].pid, resources=limits,
                      enforcement='cgroup_v2' if cgroup_root else 'admission_only', context_refs=refs)
            for key, (handle, task, limits) in list(running.items()):
                proc = handle['proc']
                expired = proc.poll() is None and time.monotonic() - handle['started'] >= task.get('timeout', timeout)
                if proc.poll() is None and not expired:
                    continue
                code, diagnostic, usage = executor.cleanup(handle)
                # 只有清理成功后才释放预算。
                for k in used:
                    used[k] -= limits[k]
                del running[key]
                result, error = {}, None
                oom = re.search(r'^oom_kill\s+([0-9]+)', usage.get('memory.events', ''), re.M)
                if oom and int(oom.group(1)):
                    error = 'OOM: cgroup 内存超限，不按原预算盲目重试'
                result_file = handle['directory'] / 'result.json'
                if code == 0 and not expired:
                    try:
                        result = json.loads(result_file.read_text(encoding='utf-8'))
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
                    except (ValueError, OSError, TypeError, KeyError) as exc:
                        error = f'协议错误: {exc}'
                state = 'timeout' if expired else ('succeeded' if code == 0 and error is None else 'failed')
                if state in ('failed', 'timeout') and not error and attempts[key] <= task.get('retries', 0):
                    event(key, 'waiting', reason='清理完成，创建新 Attempt 重试', exit_code=code, usage=usage)
                else:
                    event(key, state, exit_code=code, output=diagnostic, result=result,
                          error=error, usage=usage)
            if running:
                time.sleep(0.02)
    finally:
        try:
            for handle, _, _ in list(running.values()):
                executor.cleanup(handle)
        finally:
            journal.close()
    (root / 'summary.json').write_text(json.dumps({'states': states, 'attempts': attempts},
                                                ensure_ascii=False, indent=2), encoding='utf-8')
    return states, events
