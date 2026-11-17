"""Worker-side service managers; authority stays in the existing gateways.

The dispatcher derives the service from registered operation names. A model
cannot select a more privileged manager with a `service` field in its payload.
"""
import json
import re
from dataclasses import dataclass
from .syscall import service_for


@dataclass
class ManagedResult:
    value:object=None
    model_call:dict=None


def execute_plugin(task):
    from ..capabilities import execute_plugin as invoke
    try:
        output=invoke(task)
        if task.get('expect_failure') and task.get('plugin')=='command.run':
            output['expected_failure_observed']=bool(output.get('executed') and output.get('exit_code') not in (None,0))
        return output
    except (ValueError,SyntaxError,OSError,RuntimeError,ArithmeticError) as exc:
        detail=str(exc)[:500] if type(exc) is ValueError else '工具未完成，按类型检查参数、权限或运行结果'
        detail=re.sub(r'(?i)sk-[a-z0-9_-]{12,}|bearer\s+\S+|(?:api[_-]?key|password|secret|token)\s*[=:]\s*[^\s,;]+','[redacted]',detail)
        return {'plugin':task.get('plugin'),'ok':False,'executed':False,'error_type':type(exc).__name__,
                'error_code':'TOOL_INVALID_ARGUMENTS' if isinstance(exc,(ValueError,SyntaxError,ArithmeticError)) else 'TOOL_EXECUTION_REJECTED',
                'error':('参数必须使用合法的有限算术表达式，不能使用英文描述、变量或占位句。' if isinstance(exc,SyntaxError) and task.get('plugin')=='math.calculate' else detail)}


class LLMManager:
    service='llm'
    @staticmethod
    def execute(task,*,rpc=None,context_view=None,images=()):
        messages=list(task.get('messages',[]));images=list(images);contexts=[]
        for context in task.get('_contexts',[]):
            if isinstance(context,dict) and '_images' in context:
                images.extend(context['_images']);contexts.append({k:v for k,v in context.items() if k!='_images'})
            else:contexts.append(context)
        if images:messages.append({'role':'system','content':'agentrt-internal-images-v1:'+json.dumps(images)})
        if task.get('_contexts') or task.get('_inputs'):
            body={'context':contexts,'upstream_results':task.get('_inputs',{})}
            if context_view and context_view['body'].get('context_control'):body['context_control']=context_view['body']['context_control']
            messages.append({'role':'user','content':json.dumps(body,ensure_ascii=False)})
        from ..model_exchange import complete
        return ManagedResult(model_call=complete(task,messages))


class ToolManager:
    service='tool'
    @staticmethod
    def execute(task,**_):return ManagedResult(value=execute_plugin(task))


class StorageManager(ToolManager):
    service='storage'


class ContextManager(ToolManager):
    service='context'


class MemoryManager(ToolManager):
    service='memory'
    @staticmethod
    def execute(task,*,rpc=None,**_):
        action=task.get('action')
        if action=='plugin':return ManagedResult(value=execute_plugin(task))
        fields={'memory_write':('content','policy'),'memory_read':('budget',),'memory_delete':('memory_id',)}
        arguments={'op':action}
        for name in fields[action]:
            if name in task:arguments['id' if name=='memory_id' else name]=task[name]
        return ManagedResult(value=rpc(task,**arguments))


MANAGERS={manager.service:manager for manager in (LLMManager,ToolManager,StorageManager,ContextManager,MemoryManager)}


def managed(task):return task.get('action') in ('llm','plugin','memory_write','memory_read','memory_delete')


def execute(task,**options):
    if not managed(task):raise ValueError('Operation has no service manager')
    return MANAGERS[service_for(task)].execute(task,**options)
