"""Metadata-only system call identity; payloads and credentials stay elsewhere."""
from dataclasses import dataclass,asdict

SERVICES=('llm','tool','memory','storage','context','process','runtime')
STORAGE_PLUGINS=frozenset(('file.read','file.write','file.copy','file.replace','file.list',
    'artifact.write','workspace.inspect','workspace.import','workspace.apply_patch'))
MEMORY_PLUGINS=frozenset(('memory.remember','memory.recall','memory.forget'))


def service_for(task):
    action=task.get('action','echo');plugin=task.get('plugin','')
    if not isinstance(plugin,str):plugin=''
    if action=='llm':return 'llm'
    if action in ('memory_write','memory_read','memory_delete') or action=='plugin' and plugin in MEMORY_PLUGINS:return 'memory'
    if action=='plugin' and plugin in STORAGE_PLUGINS:return 'storage'
    if action=='plugin' and plugin.startswith('context.'):return 'context'
    if action=='plugin':return 'tool'
    if action=='command':return 'process'
    return 'runtime'


@dataclass
class Syscall:
    id:str
    task_id:str
    agent:str
    service:str
    operation:str
    attempt:int
    created:float
    state:str='dispatching'
    started:float=None
    finished:float=None
    pid:int=None
    wait_seconds:float=0.

    def public(self,now):
        value=asdict(self)
        value['execution_seconds']=max(0.,(self.finished if self.finished is not None else now)-self.started) if self.started is not None else None
        return value
