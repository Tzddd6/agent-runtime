"""Bounded conversation-driven control, adapted from AutoGen's pattern.

This module independently implements a small trusted template; it does not embed
AutoGen or let a model choose commands, executable task fields, or unbounded turns.
"""
import json


def review_tasks(prompt, round_number=1, previous=None):
    if type(round_number) is not int or not 1<=round_number<=2:
        raise ValueError('评审最多两轮')
    writer, critic, gate = (f'{name}-{round_number}' for name in ('writer','critic','gate'))
    deps = previous or []
    return [
        {'id':writer,'agent':'writer','action':'llm','timeout':75,'max_tokens':512,
         'depends_on':deps,'inputs':[d for d in deps if not d.startswith('gate-')],
         'contexts':[{'user_request':prompt}], 'messages':[{'role':'system','content':
            '回答用户任务；如存在上轮答案和评审意见，依据意见修订。仅输出答案正文。'}]},
        {'id':critic,'agent':'critic','action':'llm','timeout':75,'max_tokens':384,
         'depends_on':[writer],'inputs':[writer],'messages':[{'role':'system','content':
            '审查答案的准确性和完整性。仅输出JSON对象，不要Markdown代码块：'
            '{"decision":"approved或revise","feedback":"具体意见"}。decision只能为approved或revise。'}]},
        {'id':gate,'agent':'router','action':'review_gate','depends_on':[writer,critic],
         'inputs':[writer,critic],'review_round':round_number,'review_prompt':prompt,
         'writer_task':writer,'critic_task':critic,
         'resources':{'cpu_milli':100,'memory_bytes':64*1024**2,'pids':8}}
    ]


def route_review(task):
    round_number = task['review_round']
    if type(round_number) is not int or not 1<=round_number<=2:
        raise ValueError('评审轮数非法')
    inputs = task['_inputs']
    critique = json.loads(inputs[task['critic_task']])
    if (not isinstance(critique,dict) or critique.get('decision') not in ('approved','revise')
            or not isinstance(critique.get('feedback'),str) or len(critique['feedback'])>4000):
        raise ValueError('评审输出不满足结构化协议')
    accepted = critique['decision']=='approved'
    terminal = accepted or round_number==2
    result = {'accepted':accepted,'round':round_number,'feedback':critique['feedback'],
              'termination':'approved' if accepted else 'max_rounds' if terminal else 'revision_requested',
              'answer':inputs[task['writer_task']]}
    children=[] if terminal else review_tasks(task['review_prompt'], round_number+1,
        [task['id'],task['writer_task'],task['critic_task']])
    return result,children
