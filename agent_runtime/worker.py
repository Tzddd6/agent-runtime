"""每个任务在单独 Python 进程中执行；目前只提供确定性演示动作。"""
import json
import sys
import time
import subprocess
from pathlib import Path
import urllib.request
import collections
import hashlib
import re


def ipc(task, **value):
    request = urllib.request.Request(task['_ipc_url'], data=json.dumps(value).encode(),
        headers={'Authorization': 'Bearer ' + task['_ipc_token'], 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=6) as response:
        return json.load(response)


def main():
    task = json.loads(sys.stdin.read())
    action = task.get('action', 'echo')
    model_result = None
    output = task.get('message', '完成')
    if task.get('_attempt', 1) <= task.get('fail_attempts', 0):
        print('模拟可恢复失败', file=sys.stderr)
        return 1
    if action == 'sleep':
        time.sleep(float(task.get('seconds', 0)))
    elif action == 'fail':
        print('模拟 Worker 失败', file=sys.stderr)
        return 1
    elif action == 'llm':
        messages = list(task.get('messages', []))
        if task.get('_contexts') or task.get('_inputs'):
            messages.append({'role':'user', 'content':json.dumps(
                {'context':task.get('_contexts', []), 'upstream_results':task.get('_inputs', {})},
                ensure_ascii=False)})
        request = urllib.request.Request(task['_gateway_url'],
            data=json.dumps({'messages':messages, 'max_tokens':task.get('max_tokens',256)}).encode(),
            headers={'Authorization':'Bearer '+task['_gateway_token'], 'Content-Type':'application/json'})
        # 只请求控制进程的本地网关，不直接访问供应商或读取其密钥。
        with urllib.request.urlopen(request, timeout=65) as response:
            model_result = json.load(response)
    elif action == 'command':
        argv = task.get('argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(s, str) for s in argv):
            raise ValueError('argv 必须为非空字符串数组')
        # 仅执行受信任配置中的命令；不解析 shell，不接受模型输出直接执行。
        completed = subprocess.run(argv, cwd=task['_workspace'])
        if completed.returncode:
            return 1
    elif action == 'send':
        output = ipc(task, op='send', to=task['to'], data=task['data'],
                     request_id=task.get('request_id', task['id']))
    elif action == 'receive':
        deadline = time.monotonic() + float(task.get('seconds', 5))
        while True:
            receipt = ipc(task, op='receive')['message']
            if receipt is not None:
                output = ipc(task, op='get', ref=receipt['ref'])['data']
                ipc(task, op='ack', ref=receipt['ref'], request_id=receipt['request_id'])
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('等待 Agent 消息超时')
            time.sleep(.05)
    elif action in ('analyze_text', 'index_text'):
        text = '\n'.join(item['text'] for item in task.get('_contexts', []))
        words = re.findall(r'\w+', text.lower())
        if action == 'analyze_text':
            output = {'characters': len(text), 'words': len(words), 'lines': len(text.splitlines()),
                      'sha256': hashlib.sha256(text.encode()).hexdigest()}
        else:
            output = {'top_words': collections.Counter(words).most_common(10),
                      'unique_words': len(set(words))}
    elif action == 'review_document':
        output = {'report': task.get('_inputs', {}), 'reviewer': task.get('agent', task['id'])}
        Path(task['_workspace'], 'report.json').write_text(
            json.dumps(output, ensure_ascii=False, indent=2), encoding='utf-8')
    elif action != 'echo':
        print(f'未知动作: {action}', file=sys.stderr)
        return 2
    result = {'task_id': task['id'], 'result': output,
              'contexts': task.get('_contexts', []), 'spawn': task.get('spawn', [])}
    if model_result is not None:
        result['result'] = model_result['content']
        result['model_call'] = model_result
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'task_id': task['id'], 'result': result['result']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
