import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def validate(tasks):
    ids = [t['id'] for t in tasks]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('任务 ID 必须非空且唯一')
    pending = {t['id']: set(t.get('depends_on', [])) for t in tasks}
    if any(deps - set(ids) for deps in pending.values()):
        raise ValueError('存在不存在的依赖')
    done = set()
    while pending:
        ready = [key for key, deps in pending.items() if deps <= done]
        if not ready:
            raise ValueError('任务依赖存在环')
        for key in ready:
            del pending[key]
            done.add(key)


def run(tasks, max_workers=2, timeout=10):
    validate(tasks)
    if max_workers < 1 or timeout <= 0:
        raise ValueError('并发数和超时时间必须大于 0')
    states = {task['id']: 'waiting' for task in tasks}
    running = {}
    events = []

    def event(task_id, state, **extra):
        states[task_id] = state
        events.append({'task_id': task_id, 'state': state, **extra})
        print(json.dumps(events[-1], ensure_ascii=False), flush=True)

    while any(s in ('waiting', 'running') for s in states.values()):
        for task in tasks:
            task_id = task['id']
            if states[task_id] != 'waiting':
                continue
            deps = task.get('depends_on', [])
            if any(states[d] in ('failed', 'skipped', 'timeout') for d in deps):
                event(task_id, 'skipped', reason='依赖未成功')
            elif len(running) < max_workers and all(states[d] == 'succeeded' for d in deps):
                output = tempfile.TemporaryFile(mode='w+', encoding='utf-8')
                process = subprocess.Popen([sys.executable, '-X', 'utf8', '-m', 'agent_runtime.worker'],
                    stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
                process.stdin.write(json.dumps(task, ensure_ascii=False))
                process.stdin.close()
                running[task_id] = (process, time.monotonic(), output)
                event(task_id, 'running', pid=process.pid)
        for task_id, (process, started, output) in list(running.items()):
            code = process.poll()
            timed_out = code is None and time.monotonic() - started >= timeout
            if timed_out:
                process.kill()
                code = process.wait()
            if code is not None:
                output.seek(0)
                result = output.read().strip()
                output.close()
                event(task_id, 'timeout' if timed_out else ('succeeded' if code == 0 else 'failed'), output=result)
                del running[task_id]
        if running:
            time.sleep(0.02)
    return states, events


DEMO = [
    {'id': 'plan', 'message': '制定计划'},
    {'id': 'analyze', 'depends_on': ['plan'], 'action': 'sleep', 'seconds': 0.2, 'message': '分析代码'},
    {'id': 'test_plan', 'depends_on': ['plan'], 'action': 'sleep', 'seconds': 0.2, 'message': '准备测试'},
    {'id': 'review', 'depends_on': ['analyze', 'test_plan'], 'message': '汇总结果'},
]


def main():
    parser = argparse.ArgumentParser(description='最小 Agent Runtime 演示')
    parser.add_argument('--tasks', type=Path, help='JSON 任务数组；省略则运行内置示例')
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--timeout', type=float, default=10)
    args = parser.parse_args()
    try:
        tasks = json.loads(args.tasks.read_text(encoding='utf-8')) if args.tasks else DEMO
        states, _ = run(tasks, args.workers, args.timeout)
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0 if all(s == 'succeeded' for s in states.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
