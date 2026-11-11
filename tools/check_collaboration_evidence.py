"""Check observable conversation receipts; never infer model-internal causality."""
import argparse
import json
import math
from pathlib import Path
import re


TEAM_DEPS={'plan':[], 'analyze':['plan'], 'test_plan':['plan'], 'review':['analyze','test_plan']}
BOUNDARY=('Matching independently generated upstream tags is observable receipt evidence, '
          'not proof of internal reasoning, factual correctness, or model-internal causality.')


def parse_object(value):
    if isinstance(value,dict):return value
    if not isinstance(value,str):raise ValueError('model output is not text or an object')
    value=value.strip()
    if value.startswith('```'):
        match=re.fullmatch(r'```(?:json)?\s*\n([\s\S]*?)\n```',value)
        if not match:raise ValueError('model output is not a single JSON block')
        value=match.group(1)
    parsed=json.loads(value)
    if not isinstance(parsed,dict):raise ValueError('model output is not an object')
    return parsed


def successful_events(detail):
    return {e['task_id']:e for e in detail.get('events',[])
            if isinstance(e.get('task_id'),str) and e.get('state')=='succeeded' and isinstance(e.get('result'),dict)}


def model_usage(detail):
    calls=[]
    totals={k:0 for k in ('prompt_tokens','completion_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens')}
    for event in detail.get('events',[]):
        call=(event.get('result') or {}).get('model_call') if isinstance(event.get('result'),dict) else None
        if not isinstance(call,dict):continue
        usage=call.get('usage',{})
        usage=usage if isinstance(usage,dict) else {}
        clean={k:v for k,v in usage.items() if type(v) in (int,float) and math.isfinite(v) and v>=0}
        for key in totals:totals[key]+=clean.get(key,0)
        latency=call.get('latency_seconds')
        calls.append({'task_id':event.get('task_id'),'attempt':event.get('attempt'),
                      'model':call.get('model'),'usage':clean,
                      'latency_seconds':latency if type(latency) in (int,float) and math.isfinite(latency) and latency>=0 else None})
    return {'recorded_model_calls':len(calls),'calls':calls,'totals':totals,
            'usage_complete':bool(calls) and all('prompt_tokens' in c['usage'] and 'completion_tokens' in c['usage'] for c in calls),
            'billing_boundary':'Successful response usage only; failed/incomplete calls may still be billable. Not a provider bill.'}


def check_tag_flow(detail):
    completed=successful_events(detail)
    findings=[]
    parsed={}
    for task in TEAM_DEPS:
        event=completed.get(task)
        if event is None:
            findings.append(f'{task}: no successful result');continue
        try:value=parse_object(event['result'].get('result'))
        except (ValueError,TypeError) as exc:
            findings.append(f'{task}: invalid structured receipt ({exc})');continue
        if not isinstance(value.get('tag'),str) or not re.fullmatch(r'[A-Za-z0-9]{8}',value['tag']):
            findings.append(f'{task}: tag must contain eight ASCII letters/digits')
        if not isinstance(value.get('received_tags'),list) or not all(isinstance(t,str) for t in value['received_tags']):
            findings.append(f'{task}: received_tags must be a string list')
        if not isinstance(value.get('answer'),str) or not value['answer'].strip():
            findings.append(f'{task}: missing role answer')
        parsed[task]=value
    tags=[v.get('tag') for v in parsed.values() if isinstance(v.get('tag'),str)]
    if len(tags)!=len(set(tags)):findings.append('role tags are not independently distinct')
    receipts=[]
    for task,deps in TEAM_DEPS.items():
        if task not in parsed or any(d not in parsed for d in deps):continue
        expected=[parsed[d].get('tag') for d in deps]
        received=parsed[task].get('received_tags')
        matched=(isinstance(received,list) and all(isinstance(t,str) for t in received)
                 and all(isinstance(t,str) for t in expected)
                 and len(received)==len(expected) and sorted(received)==sorted(expected))
        if not matched:findings.append(f'{task}: upstream tag receipt mismatch')
        starts=[e for e in detail.get('events',[]) if e.get('task_id')==task and e.get('state')=='running']
        ordered=bool(starts) and all(type(starts[0].get('time')) in (int,float)
            and type(completed[d].get('time')) in (int,float)
            and starts[0]['time']>=completed[d]['time'] for d in deps)
        if not ordered:findings.append(f'{task}: dependency ordering not established')
        receipts.append({'task_id':task,'expected_upstream_tags':expected,'received_tags':received,
                         'matches':matched,'dependency_ordered':ordered})
    states=detail.get('states',{})
    if any(states.get(t)!='succeeded' for t in TEAM_DEPS):findings.append('not every expected task state succeeded')
    return {'kind':'live_tag_receipt_check','passed':not findings,'findings':findings,
            'tags':{key:value.get('tag') for key,value in parsed.items()},'receipts':receipts,
            'usage':model_usage(detail),'claim_boundary':BOUNDARY}


def check_team(detail,receipts=None):
    if receipts is None:return check_tag_flow(detail)
    findings=[]
    completed=successful_events(detail)
    for task,marker in receipts.items():
        if task not in completed or marker not in str(completed[task]['result'].get('result','')):
            findings.append(f'{task}: expected marker not observed')
    return {'kind':'caller_supplied_marker_check','passed':not findings,'findings':findings,
            'claim_boundary':BOUNDARY+' Caller-supplied markers known in the original prompt are weaker evidence.',
            'usage':model_usage(detail)}


def check_review(detail):
    completed=successful_events(detail)
    gates=[];findings=[]
    for key,event in completed.items():
        if not key.startswith('gate-'):continue
        try:value=parse_object(event['result'].get('result'))
        except (ValueError,TypeError):findings.append(f'{key}: malformed gate result');continue
        round_number=value.get('round');termination=value.get('termination');accepted=value.get('accepted')
        valid=(type(round_number) is int and round_number in (1,2) and type(accepted) is bool
               and ((termination=='approved' and accepted)
                    or (termination=='revision_requested' and round_number==1 and not accepted)
                    or (termination=='max_rounds' and round_number==2 and not accepted)))
        if not valid:findings.append(f'{key}: inconsistent round/termination/accepted contract')
        gates.append({'task_id':key,'round':round_number,'termination':termination,'accepted':accepted,'valid':valid})
        if valid:
            if key!=f'gate-{round_number}':findings.append(f'{key}: task ID does not match round')
            for role in ('writer','critic','gate'):
                expected=f'{role}-{round_number}'
                if detail.get('states',{}).get(expected)!='succeeded' or expected not in completed:
                    findings.append(f'{expected}: expected round participant not successful')
            critic=completed.get(f'critic-{round_number}')
            if critic is not None:
                try:
                    decision=parse_object(critic['result'].get('result')).get('decision')
                    expected=('approved' if decision=='approved' else
                              'revision_requested' if decision=='revise' and round_number==1 else
                              'max_rounds' if decision=='revise' else None)
                    if expected is None or expected!=termination:
                        findings.append(f'{key}: gate does not match actual critic decision')
                except (ValueError,TypeError):findings.append(f'{key}: critic response cannot establish routing evidence')
    gates.sort(key=lambda g:g['round'] if type(g['round']) is int else 999)
    if not gates:findings.append('no successful review gate result')
    if gates and gates[0]['round']!=1:findings.append('first gate is not round one')
    if any(g['termination']=='revision_requested' for g in gates) and not any(g['round']==2 for g in gates):
        findings.append('revision requested but round two was not observed')
    if any(g['termination']=='approved' and g['round']==1 for g in gates) and len(gates)>1:
        findings.append('additional rounds followed approval')
    if gates and gates[-1]['termination'] not in ('approved','max_rounds'):
        findings.append('terminal decision not observed')
    if any(value!='succeeded' for value in detail.get('states',{}).values()):
        findings.append('workflow has non-success task states')
    return {'kind':'live_review_gate_check','passed':not findings,'findings':findings,'gates':gates,
            'observed_approved':any(g['termination']=='approved' for g in gates),
            'observed_revision':any(g['termination']=='revision_requested' for g in gates),
            'observed_max_rounds':any(g['termination']=='max_rounds' for g in gates),
            'usage':model_usage(detail),
            'claim_boundary':'Only observed live paths are covered. Unobserved revision/max-round paths require separate offline tests.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('detail',type=Path)
    parser.add_argument('--kind',choices=('team','review'),default='team')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    detail=json.loads(args.detail.read_text(encoding='utf-8-sig'))
    result=(check_tag_flow if args.kind=='team' else check_review)(detail)
    raw=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(raw,encoding='utf-8')
    print(raw)
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
