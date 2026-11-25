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
        from aios.tool.engineering import manifest, sanitize_context_for_model
        return sanitize_context_for_model(manifest(params.get('project', 'pricing'), include_reference=False))
    else:
        raise ValueError('Tool has no bound implementation')
    result = kernel.execute_request(agent, query)['response']
    if result.status_code >= 400 or result.error:
        raise RuntimeError('Kernel subsystem rejected operation')
    return result.response_message


BOUND_TOOLS = {'memory.remember', 'memory.recall', 'memory.forget', 'file.read', 'file.write',
               'file.list', 'artifact.write', 'context.read', 'runtime.observe', 'project.inspect'}
