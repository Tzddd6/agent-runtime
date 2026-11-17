"""每个任务在单独 Python 进程中执行；目前只提供确定性演示动作。"""
import json
import sys
import time
import subprocess
from pathlib import Path
import urllib.request
import urllib.error
import collections
import hashlib
import re


def ipc(task, **value):
    request = urllib.request.Request(task['_ipc_url'], data=json.dumps(value,ensure_ascii=False).encode(),
        headers={'Authorization': 'Bearer ' + task['_ipc_token'], 'Content-Type': 'application/json'})
    from .model_exchange import LocalNoRedirect
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),LocalNoRedirect())
    with opener.open(request, timeout=6) as response:
        return json.load(response)

def perform_plugin(task):
    from .kernel.managers import execute_plugin
    return execute_plugin(task)


def main():
    if len(sys.argv)>2:
        if sys.stdin.read(1) != 'G':return 2
        source=Path(sys.argv[2])
        try:
            with source.open('rb') as payload_file:raw=payload_file.read(16*1024**2+1)
            if len(raw)>16*1024**2:raise ValueError('Worker payload 超过 16 MiB')
            task=json.loads(raw)
        finally:source.unlink(missing_ok=True)
    else:
        # Compatibility for the original trusted JSON-stdin entry point.
        task = json.loads(sys.stdin.read())
    action = task.get('action', 'echo');context_view=None;view_images=[]
    # Model tasks receive one bounded authenticated view rather than N full RPCs.
    if action=='llm' and (task.get('_context_refs') or task.get('_input_refs')):
        context_view=ipc(task,op='context_view',context_refs=task.get('_context_refs',[]),input_refs=task.get('_input_refs',{}),
                         query=task.get('_context_query') or '\n'.join(m.get('content','') for m in task.get('messages',[]) if isinstance(m,dict))[:2000])
        task['_contexts']=context_view['body']['context'];task['_inputs']=context_view['body']['upstream_results'];view_images=context_view['images']
    # Transport references, materialize only inside the consuming Attempt.
    # Broker binds the bearer capability to the real Agent identity.
    if '_context_refs' in task and context_view is None:
        if task.get('_context_refs') or task.get('_input_refs'):
            batch=ipc(task,op='materialize',context_refs=task.get('_context_refs',[]),input_refs=task.get('_input_refs',{}))
            task['_contexts']=batch['context'];task['_inputs']=batch['upstream_results']
        else:task['_contexts']=[];task['_inputs']={}
    action = task.get('action', 'echo')
    model_result = None
    output = task.get('message', '完成')
    children = task.get('spawn', [])
    if task.get('_attempt', 1) <= task.get('fail_attempts', 0):
        print('模拟可恢复失败', file=sys.stderr)
        return 1
    from .kernel.managers import managed,execute
    if managed(task):
        from .model_exchange import CompletionFailure
        try:
            managed_result=execute(task,rpc=ipc,context_view=context_view,images=view_images)
            output=managed_result.value;model_result=managed_result.model_call
        except CompletionFailure as exc:
            from .failures import failure
            if len(sys.argv)>1:Path(sys.argv[1]).write_text(json.dumps({'task_id':task['id'],'failure':failure(exc.code)}))
            print(failure(exc.code)['message'],file=sys.stderr)
            return 1
    elif action == 'sleep':
        time.sleep(float(task.get('seconds', 0)))
    elif action=='clock':
        from .clock_tool import now
        output=now(task.get('timezone','Asia/Shanghai'))['display']
    elif action == 'load':
        seconds, memory = float(task.get('seconds',1)), task.get('memory_mib',16)
        if not 0<seconds<=20 or type(memory) is not int or not 1<=memory<=64:
            raise ValueError('资源演示限于 20 秒、64 MiB 以内')
        block=bytearray(memory*1024**2)
        for offset in range(0,len(block),4096):block[offset]=1
        end=time.monotonic()+seconds
        counter=0
        while time.monotonic()<end:
            interval=min(end,time.monotonic()+.06)
            while time.monotonic()<interval:counter=(counter+1)%1000003
            time.sleep(.04)
        output={'allocated_mib':memory,'work_units':counter,'seconds':seconds}
    elif action == 'capabilities':
        from .runtime_capabilities import network_answer
        output=network_answer()
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
    elif action == 'send':
        output = ipc(task, op='send', to=task['to'], data=task['data'],
                     request_id=task.get('request_id', task['id']))
    elif action == 'receive':
        deadline = time.monotonic() + float(task.get('seconds', 5))
        while True:
            receipt = ipc(task, op='receive')['message']
            if receipt is not None:
                output = ipc(task, op='get', ref=receipt['ref'])['data']
                ipc(task, op='ack', message_id=receipt['message_id'])
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('等待 Agent 消息超时')
            time.sleep(.05)
    elif action == 'route_gate':
        from .routing import route_plan,repair_plan
        try:output, children = route_plan(task)
        except ValueError as exc:output,children=repair_plan(task,exc)
    elif action == 'web_gate':
        from .web_flow import finish
        output,children=finish(task)
    elif action == 'artifact_gate':
        from .svg_workflow import finish
        output,children=finish(task)
    elif action in ('repair_prepare','repair_check'):
        from .repair_loop import execute
        output,children=execute(task)
    elif action == 'review_gate':
        from .conversation import route_review
        output, children = route_review(task)
    elif action in ('project_baseline','project_validate','project_gate'):
        from .project_workflow import execute_project_action
        output, children = execute_project_action(task)
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
              'context_refs':task.get('_context_refs',[]),'spawn':children}
    if model_result is not None:
        result['result'] = model_result['content']
        result['model_call'] = model_result
        if context_view:result['context_view']=context_view['stats']
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'task_id': task['id'], 'result': result['result']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
