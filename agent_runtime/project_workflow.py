"""Trusted seven-stage repair workflow for a fixed project registry.

Models provide text/JSON only; this module alone decides which tasks can run.
"""
import json
from pathlib import Path
from .engineering import manifest,project_metadata,project_context,validate_patch,run_check


def counts(report):
    if not isinstance(report,dict):return None
    return {k:report[k] for k in ('total','passed','failed','errors','success','timed_out') if k in report}


def project_tasks(project):
    data=manifest(project)
    context={'project':data}
    common={'cpu_milli':500,'memory_bytes':128*1024**2,'pids':16}
    def llm(key,role,deps,tokens,message):
        return {'id':key,'agent':role,'action':'llm','depends_on':deps,'inputs':deps,
                'contexts':[context],'max_tokens':tokens,'timeout':75,'resources':dict(common),
                'messages':[{'role':'system','content':message}]}
    return [
        {'id':'baseline','agent':'test_runner','action':'project_baseline','project':project,
         'timeout':15,'resources':{'cpu_milli':1000,'memory_bytes':192*1024**2,'pids':16}},
        llm('analyze','analyst',['baseline'],256,
            'Analyze the supplied small Python project and diagnose its defects. Return concise text, no code.'),
        llm('test_plan','tester',['baseline'],384,
            'Propose concise boundary tests for the supplied requirements. Existing fixed tests cannot be changed.'),
        llm('repair','repairer',['analyze','test_plan'],1024,
            'Repair the supplied project using the upstream analysis and test advice. '
            'Return ONLY one JSON object {"source":"complete corrected module"}; no Markdown or explanation. '
            'Preserve exactly the supplied signatures and obey the listed AST restrictions. '
            'No imports, files, network, commands, loops or test modifications.'),
        {'id':'validate','agent':'test_runner','action':'project_validate','project':project,
         'depends_on':['repair'],'inputs':['repair'],'timeout':15,
         'resources':{'cpu_milli':1000,'memory_bytes':192*1024**2,'pids':16}}
    ]


def execute_project_action(task):
    project=task['project']
    work=Path(task['_workspace'])/'checks'
    if task['action']=='project_baseline':
        report=run_check(manifest(project)['source'],work,project=project)
        if report['baseline_failing_count']<=0:
            raise ValueError('Original fixture is unexpectedly green; refusing model calls')
        return {'expected_red':True,'baseline_failing_count':report['baseline_failing_count'],
                'public':counts(report['baseline'].get('public')),
                'heldout':counts(report['baseline'].get('heldout'))},[]
    if task['action']=='project_validate':
        try:
            source=validate_patch(task['_inputs']['repair'],project=project)
            report=run_check(source,work,project=project)
            result={'success':report['success'],'source':source,'source_sha256':report['source_sha256'],
                    'public':counts(report.get('public')),'heldout':counts(report.get('heldout')),
                    'baseline_failing_count':report['baseline_failing_count']}
        except (ValueError,TypeError,KeyError) as exc:
            return {'success':False,'error':str(exc)[:500]},[]
        if not result['success']:return result,[]
        # This branch is selected by code and observed tests, never a model tool request.
        review={'id':'review','agent':'reviewer','action':'llm','depends_on':['validate'],
                'inputs':['validate'],'contexts':[{'requirements':manifest(project)['requirements']}],
                'max_tokens':384,'timeout':75,
                'resources':{'cpu_milli':500,'memory_bytes':128*1024**2,'pids':16},
                'messages':[{'role':'system','content':'Review the actual source and immutable test counts. '
                    'Return ONLY JSON {"accepted":true or false,"source_sha256":"copy exact hash",'
                    '"reason":"short explanation"}. Do not approve unless source meets requirements and tests pass.'}]}
        gate={'id':'gate','agent':'verdict','action':'project_gate','project':project,
              'depends_on':['validate','review'],'inputs':['validate','review'],'timeout':5,
              'resources':{'cpu_milli':100,'memory_bytes':64*1024**2,'pids':8}}
        return result,[review,gate]
    if task['action']=='project_gate':
        validation=task['_inputs']['validate']
        try:
            verdict=json.loads(task['_inputs']['review'])
            if not isinstance(verdict,dict):raise ValueError('review must be an object')
            accepted=(validation.get('success') is True and verdict.get('accepted') is True
                      and verdict.get('source_sha256')==validation.get('source_sha256'))
            reason=verdict.get('reason','')
            if not isinstance(reason,str):raise ValueError('review reason must be text')
            if verdict.get('source_sha256')!=validation.get('source_sha256'):
                reason='源码哈希不匹配；'+reason
            return {'accepted':accepted,'reason':reason[:1000],
                    'source_sha256':validation.get('source_sha256')},[]
        except (ValueError,TypeError,KeyError):
            return {'accepted':False,'reason':'最终评审协议无效，不能视为修复成功'},[]
    raise ValueError('Unknown project action')


def project_view(project,events,states,budget=None):
    outputs={e['task_id']:e['result']['result'] for e in events
             if e['state']=='succeeded' and isinstance(e.get('result'),dict)}
    baseline=outputs.get('baseline')
    validation=outputs.get('validate')
    verdict=outputs.get('gate')
    phases=[]
    for key,label in [('baseline','原始失败'),('analyze','问题分析'),('test_plan','测试设计'),
                      ('repair','生成补丁'),('validate','真实测试'),('review','独立评审'),('gate','最终结论')]:
        state=states.get(key,'waiting')
        if key not in states and any(s=='interrupted' for s in states.values()):state='interrupted'
        if key not in states and any(s=='cancelled' for s in states.values()):state='cancelled'
        elif key not in states and any(s in ('failed','timeout','skipped','unschedulable') for s in states.values()):state='skipped'
        if key in ('review','gate') and isinstance(validation,dict) and not validation.get('success'):
            state='skipped'
        if key=='validate' and isinstance(validation,dict) and not validation.get('success'):state='failed'
        if key=='gate' and isinstance(verdict,dict) and not verdict.get('accepted'):state='failed'
        details=outputs.get(key)
        phases.append({'id':key,'label':label,'status':state,'details':details})
    usage={}
    calls=0
    for event in events:
        call=(event.get('result') or {}).get('model_call',{})
        if call:
            calls+=1
            for field,value in call.get('usage',{}).items():
                if type(value) is int and value>=0:usage[field]=usage.get(field,0)+value
    info=project_metadata(project)
    return {'project':project,'title':info['name'],'phases':phases,'baseline':baseline,
            'validation':validation,'patch_source':validation.get('source') if isinstance(validation,dict) else None,
            'source_sha256':validation.get('source_sha256') if isinstance(validation,dict) else None,
            'original_source':manifest(project)['source'],'verdict':verdict,
            'usage':usage,'model_calls':calls,'call_limit':4,'max_output_tokens':2048,'budget':budget}
