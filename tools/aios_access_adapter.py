"""Test-only AIOS API bridge. Does not implement AIOS native packages.

DTOs/manager are transport facades. Authorization, ordering, deduplication,
budgeting and reads execute agent_runtime.memory_access / ContextStore.
Namespace aliases are installed only in the isolated runner process.
"""
import sys
import types
import tempfile
from pathlib import Path
from agent_runtime.memory_access import MemoryContext


class DTO:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class MemoryManager:
    pass


class MemoryProvider:
    pass


class MemoryWriteBarrier:
    def __init__(self, config):
        if config.get('enabled') is not False:
            raise NotImplementedError('Adapter covers disabled barrier only')


class ContextInjector:
    def __init__(self, memory_manager, config):
        self.manager = memory_manager
        self.config = config

    def inject(self, agent_name, query, user_id=None):
        # No production fallback identity is inferred from prompt text.
        # This legacy fallback is solely the explicit upstream API contract.
        resolved = user_id if user_id is not None else agent_name
        if not self.config.get('auto_inject', False):
            return query, {'injected_count': 0, 'resolved_user_id': user_id}
        response = self.manager.provider.retrieve_memory(DTO(
            operation_type='retrieve_memory', params={'user_id': resolved,
                                                       'agent_name': agent_name}))
        if not response.success:
            raise RuntimeError('Memory provider failed')
        with tempfile.TemporaryDirectory(prefix='agentrt-aios-') as folder:
            memory = MemoryContext(Path(folder))
            rows, diag = memory.select(response.search_results, user=resolved,
                agent=agent_name, max_bytes=self.config['max_memory_tokens'],
                max_memories=self.config['max_injected_memories'],
                relevance_threshold=self.config['relevance_threshold'])
        messages = list(query.messages)
        if rows:
            messages.insert(0, {'role': 'system', 'content': ''.join(r['line'] for r in rows)})
        result = DTO(**{**query.__dict__, 'messages': messages})
        return result, {**diag, 'resolved_user_id': user_id}


def install():
    exports = {
        'cerebrum.llm.apis': {'LLMQuery': DTO},
        'cerebrum.memory.apis': {'MemoryQuery': DTO, 'MemoryResponse': DTO},
        'aios.memory.context_injector': {'ContextInjector': ContextInjector},
        'aios.memory.manager': {'MemoryManager': MemoryManager},
        'aios.memory.providers.base': {'MemoryProvider': MemoryProvider},
        'aios.memory.write_barrier': {'MemoryWriteBarrier': MemoryWriteBarrier},
    }
    for name, values in exports.items():
        parts = name.split('.')
        for count in range(1, len(parts)+1):
            path = '.'.join(parts[:count])
            if path not in sys.modules:
                module = types.ModuleType(path); module.__path__ = []
                sys.modules[path] = module
                if count > 1:
                    setattr(sys.modules['.'.join(parts[:count-1])], parts[count-1], module)
        sys.modules[name].__dict__.update(values)
