"""每个任务在单独 Python 进程中执行；目前只提供确定性演示动作。"""
import json
import sys
import time
import subprocess
from pathlib import Path


def main():
    task = json.loads(sys.stdin.read())
    action = task.get('action', 'echo')
    if task.get('_attempt', 1) <= task.get('fail_attempts', 0):
        print('模拟可恢复失败', file=sys.stderr)
        return 1
    if action == 'sleep':
        time.sleep(float(task.get('seconds', 0)))
    elif action == 'fail':
        print('模拟 Worker 失败', file=sys.stderr)
        return 1
    elif action == 'command':
        argv = task.get('argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(s, str) for s in argv):
            raise ValueError('argv 必须为非空字符串数组')
        # 仅执行受信任配置中的命令；不解析 shell，不接受模型输出直接执行。
        completed = subprocess.run(argv, cwd=task['_workspace'])
        if completed.returncode:
            return 1
    elif action != 'echo':
        print(f'未知动作: {action}', file=sys.stderr)
        return 2
    result = {'task_id': task['id'], 'result': task.get('message', '完成'),
              'contexts': task.get('_contexts', []), 'spawn': task.get('spawn', [])}
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'task_id': task['id'], 'result': result['result']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
