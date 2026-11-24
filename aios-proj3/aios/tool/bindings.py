# Project module contract / 项目模块说明
# 职责：定义受控工具与固定工程；AST限制不等价于任意代码安全沙箱。
# 主要接口：execute。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
"""Bind stateful tools to the requesting kernel and Agent identity."""
from cerebrum.memory.apis import MemoryQuery
from cerebrum.storage.apis import StorageQuery


def execute(kernel, syscall, name, params):
    agent = syscall.agent_name
    user = getattr(syscall, 'user_id', None) or 'default'
    query = None
    if name in ('memory.remember', 'memory.recall', 'memory.forget'):
        operation = {'memory.remember': 'add_memory', 'memory.recall': 'retrieve_memory', 'memory.forget': 'remove_memory'}[name]
        query = MemoryQuery(operation_type=operation, params=params, user_id=user, agent_id=agent)
    elif name in ('file.read', 'file.write', 'file.list', 'artifact.write'):
        operation = {'file.read': 'read', 'file.write': 'write', 'file.list': 'list', 'artifact.write': 'write'}[name]
        query = StorageQuery(operation_type=operation, params=params)
    elif name == 'context.read':
        return kernel.context_store.read_page(params['id'], reader_agent=agent,
                                              offset=int(params.get('offset', 0)), limit=min(8192, int(params.get('limit', 2048))))
    elif name == 'runtime.observe':
        return kernel.status_snapshot()
    elif name == 'project.inspect':
        from aios.tool.engineering import manifest
        return {key: value for key, value in manifest(params.get('project', 'pricing')).items() if key != 'reference_fix'}
    else:
        raise ValueError('Tool has no bound implementation')
    result = kernel.execute_request(agent, query)['response']
    if result.status_code >= 400 or result.error:
        raise RuntimeError('Kernel subsystem rejected operation')
    return result.response_message


BOUND_TOOLS = {'memory.remember', 'memory.recall', 'memory.forget', 'file.read', 'file.write',
               'file.list', 'artifact.write', 'context.read', 'runtime.observe', 'project.inspect'}
