"""Trusted application templates. Model output is always treated as text."""


def tasks_for(text):
    context = {'text': text}
    return [{'id':'plan', 'agent':'planner', 'spawn':[
        {'id':'analyze', 'agent':'analyzer', 'depends_on':['plan'],
         'action':'analyze_text', 'contexts':[context]},
        {'id':'index', 'agent':'indexer', 'depends_on':['plan'],
         'action':'index_text', 'contexts':[context]},
        {'id':'review', 'agent':'reviewer', 'depends_on':['analyze','index'],
         'inputs':['analyze','index'], 'action':'review_document'}]}]


def team_tasks(prompt, *, final_tokens=512):
    large=final_tokens>1024
    instructions={
        'planner':'为用户需求制定计划，直接列出最多3项步骤，合计不超过60个汉字；不展开实现，不重复题目。',
        'analyst':'依据上游计划分析实现方法和资源约束。只给最多3项关键结论，合计不超过90个汉字；不复述计划，不写完整代码。',
        'tester':'依据上游计划给出测试输入、预期与失败恢复条件。只给最多4项验收点，合计不超过100个汉字；不复述计划。',
        'reviewer':'综合上游分析和测试建议，保留关键实现、资源约束、异常处理和验收条件。逐条覆盖原始需求中明确列出的每项要求，不可用笼统概括替代具体边界条件。直接给出可执行的最终答复，最多6项、合计不超过260个汉字；无需再次展开计划，不能遗漏上游的重要风险。'}
    if large:
        instructions.update(planner='为用户复杂需求列出完整任务分解、约束和交付物，400字以内，不写最终答案。',
            analyst='依据上游计划分析实现方法、关键细节、资源约束和风险，600字以内，不复述题目。',
            tester='依据上游计划给出测试输入、预期、边界、失败恢复和验收条件，600字以内，不复述题目。',
            reviewer='综合上游分析和测试建议，完整回答用户需求，包含必要实现细节、资源约束、风险与验收条件。使用清晰的Markdown，不为简短而省略重要内容，不复述协作过程。')
    def task(key, role, deps, tokens=256):
        return {'id':key, 'agent':role, 'action':'llm', 'timeout':75,
                'resources':{'cpu_milli':250,'memory_bytes':96*1024**2,'pids':16},
                'depends_on':deps, 'inputs':deps, 'contexts':[{'user_request':prompt}],
                'max_tokens':tokens, 'messages':[{'role':'system', 'content':
                    instructions[role]}]}
    intermediate=1024 if large else 256
    return [task('plan','planner',[],intermediate), task('analyze','analyst',['plan'],intermediate),
            task('test_plan','tester',['plan'],intermediate), task('review','reviewer',['analyze','test_plan'],final_tokens)]


def resource_tasks():
    """A bounded real load lets a human watch scheduling without paying for LLMs."""
    def load(key, cpu, memory, seconds, deps):
        return {'id':key,'agent':key,'action':'load','seconds':seconds,
                'memory_mib':memory,'depends_on':deps,'timeout':30,
                'resources':{'cpu_milli':cpu,'memory_bytes':(memory+64)*1024**2,'pids':16}}
    return [load('planner',250,8,1,[]),
            load('analyzer',1000,32,7,['planner']),
            load('tester',750,48,6,['planner']),
            load('indexer',500,24,5,['planner']),
            load('reviewer',500,16,2,['analyzer','tester','indexer'])]


def agent_tasks():
    """Two independent owners, repeated tasks and aggregate caps; no model calls."""
    def task(key,owner,seconds,deps=()):
        return {'id':key,'agent':owner,'action':'load','seconds':seconds,'memory_mib':16,
                'depends_on':list(deps),'timeout':20,
                'resources':{'cpu_milli':250,'memory_bytes':64*1024**2,'pids':8}}
    tasks=[task('alpha_1','alpha',6),task('alpha_2','alpha',6),
           task('alpha_3','alpha',4,['alpha_1','alpha_2']),task('beta_1','beta',8),
           task('beta_2','beta',4,['beta_1'])]
    specs=[{'id':owner,'role':role,'max_concurrency':slots,
        'budget':{'cpu_milli':500,'memory_bytes':192*1024**2,'pids':32}}
        for owner,role,slots in [('alpha','并行计算 Agent',2),('beta','独立计算 Agent',1)]]
    return tasks,specs


def kernel_tasks():
    """Five real service queues without calling a model or writing memories."""
    from .sdk import Request,StorageQuery,ToolQuery,MemoryQuery
    requests=[
        Request('write','storage-agent',StorageQuery('write',{'path':'kernel-demo.txt','content':'Kernel service demo: storage, memory, context and tools.'})),
        Request('calculate','tool-agent',ToolQuery('math.calculate',{'expression':'(12+3)*2'})),
        Request('memory','memory-agent',MemoryQuery('read',{'budget':1024})),
        Request('context','context-agent',ToolQuery('context.search',{'query':'kernel','limit':3}),contexts=({'text':'kernel service demo with explicitly authorized context'},)),
        Request('read','storage-agent',StorageQuery('read',{'path':'kernel-demo.txt'}),depends_on=('write',))]
    tasks=[request.task() for request in requests]
    tasks.append({'id':'collect','agent':'coordinator','action':'review_document',
                  'depends_on':[task['id'] for task in tasks],'inputs':[task['id'] for task in tasks]})
    return tasks
