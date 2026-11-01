"""每个任务在单独 Python 进程中执行；目前只提供确定性演示动作。"""
import json
import sys
import time


def main():
    task = json.loads(sys.stdin.read())
    action = task.get('action', 'echo')
    if action == 'sleep':
        time.sleep(float(task.get('seconds', 0)))
    elif action == 'fail':
        print('模拟 Worker 失败', file=sys.stderr)
        return 1
    elif action != 'echo':
        print(f'未知动作: {action}', file=sys.stderr)
        return 2
    print(json.dumps({'task_id': task['id'], 'result': task.get('message', '完成')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
