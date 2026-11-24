# Project module contract / 项目模块说明
# 职责：连接常驻内核、工作流与网页；业务成功必须由真实执行结果确认。
# 主要接口：tasks。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
"""Native console scenarios with real subsystem operations."""
from cerebrum.memory.apis import MemoryQuery
from cerebrum.storage.apis import StorageQuery


def tasks(kernel, preset, identity, text):
    def tool(node, name, params, **options):
        return {'id': node, 'agent': identity + '-' + node, 'query_type': 'tool',
                'query_data': {'tool_calls': [{'name': name, 'parameters': params}], **options}}

    if preset == 'resources':
        return [tool(f'worker-{index}', 'runtime.load', {'seconds': 0.4, 'memory_mib': 8},
                     resources={'cpu_milli': 250, 'memory_bytes': 64 * 1024**2, 'pids': 4}) for index in range(4)]
    if preset == 'kernel':
        return [tool('calculate', 'math.calculate', {'expression': '(12+3)*2'}),
                {'id': 'remember', 'agent': identity, 'query': MemoryQuery(operation_type='add_memory',
                    params={'content': 'native kernel verification'}, user_id='local', agent_id=identity)},
                {'id': 'store', 'agent': identity, 'query': StorageQuery(operation_type='write',
                    params={'path': 'verification/' + identity + '.txt', 'content': 'native verification'})}]
    if preset == 'agents':
        def lifecycle(*_):
            drf = kernel.scheduler.drf_controller
            name = identity + '-controlled'
            drf.register_agent(name)
            drf.pause_agent(name)
            drf.resume_agent(name)
            drf.terminate_agent(name, ipc_manager=kernel.ipc_manager)
            return drf.snapshot()['agents'][name]
        return [{'id': 'lifecycle', 'handler': lifecycle}]
    if preset == 'ipc':
        def exchange(*_):
            ipc = kernel.ipc_manager
            first, second = identity + '-sender', identity + '-recipient'
            token1, token2 = ipc.issue_token(first), ipc.issue_token(second)
            try:
                ref = kernel.context_store.put(first, {'summary': text or 'native IPC fixture'})
                message = ipc.send(sender=first, recipient=second, payload={'reference': ref},
                                   request_id=identity, context_refs=[ref], token=token1)
                received = ipc.receive(second, token=token2)
                result = kernel.context_store.get(ref, reader_agent=second)
                ipc.ack(second, received[0]['message_id'], token=token2)
                return {'received': result, 'message_id': message['message_id'], 'acknowledged': True}
            finally:
                ipc.revoke_token(first)
                ipc.revoke_token(second)
        return [{'id': 'exchange', 'handler': exchange}]
    if preset == 'litecua':
        return [
            tool('observe_boot', 'litecua/observe', {}),
            {**tool('click_search', 'litecua/click', {'element_id': 'input_search_bar'}), 'depends_on': ['observe_boot']},
            {**tool('type_query', 'litecua/type', {'element_id': 'input_search_bar', 'text': text or 'AIOS LiteCUA', 'append': False}), 'depends_on': ['click_search']},
            {**tool('restore_boot', 'litecua/snapshot', {'action': 'restore', 'name': 'boot'}), 'depends_on': ['type_query']},
        ]
    raise ValueError('Unsupported native scenario')
