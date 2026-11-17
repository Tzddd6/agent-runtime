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


def team_tasks(prompt):
    def task(key, role, deps, tokens=256):
        return {'id':key, 'agent':role, 'action':'llm', 'timeout':75,
                'depends_on':deps, 'inputs':deps, 'contexts':[{'user_request':prompt}],
                'max_tokens':tokens, 'messages':[{'role':'system', 'content':
                    {'planner':'为用户需求制定简短计划。',
                     'analyst':'依据计划分析实现方法和系统资源需求。',
                     'tester':'依据计划给出可验证的测试与失败场景。',
                     'reviewer':'综合分析和测试建议，给出一致的最终答复。'}[role]}]}
    return [task('plan','planner',[]), task('analyze','analyst',['plan']),
            task('test_plan','tester',['plan']), task('review','reviewer',['analyze','test_plan'],512)]


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
