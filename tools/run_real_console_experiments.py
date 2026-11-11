"""Use the existing console's in-memory key without exporting or restarting it.

The live console is the only model caller. This trusted harness tests a fixed,
AST-restricted project in dedicated cgroups. Never run arbitrary model programs.
"""
import argparse
import itertools
import json
import os
import secrets
import sys
import time
import urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_runtime.engineering import BASELINE_SOURCE,project_context,run_check,validate_patch
from agent_runtime.runtime import run
from agent_runtime.web import delegated_attempts
from tools.check_collaboration_evidence import check_tag_flow,check_review,model_usage
from tools.replay_project_cache import replay


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


class Console:
    def __init__(self,root,max_calls=100):
        self.root=root
        self.token=(Path.home()/'.config/agentrt/console.env').read_text().strip().split('=',1)[1]
        self.max_calls=max_calls
        checkpoint=root/'call-budget.json'
        if checkpoint.exists():
            raise RuntimeError('Output already has a live-call checkpoint; refusing automatic re-billing')
        self.reserved=0
        self.checkpoint=checkpoint

    def request(self,path,body=None,csrf=None):
        headers={'X-AgentRT-Proxy-Token':self.token}
        if csrf:headers['X-AgentRT-CSRF']=csrf
        data=None if body is None else json.dumps(body,ensure_ascii=False).encode()
        return json.load(urllib.request.urlopen(urllib.request.Request(
            'http://127.0.0.1:18880/api/'+path,data=data,headers=headers),timeout=12))

    def submit(self,label,preset,prompt,workers=2,policy='resource_fit'):
        if len(prompt)>4000:raise ValueError('Prompt exceeds existing console input contract')
        maximum={'team':4,'review':4,'llm':1}[preset]
        if self.reserved+maximum>self.max_calls:raise RuntimeError('Live call budget exhausted')
        for _ in range(180):
            status=self.request('status')
            if status.get('recovery_required'):raise RuntimeError('Console requires recovery; no new calls issued')
            if not status['model_configured']:raise RuntimeError('Console model key not configured')
            if not any(j['status'] in ('running','cancelling') for j in status['jobs']):break
            time.sleep(1)
        else:raise RuntimeError('Console busy; no paid submission retried')
        self.reserved+=maximum
        save(self.checkpoint,{'max_calls':self.max_calls,'reserved_upper_bound':self.reserved,
                              'last_intent':label,'no_automatic_paid_retry':True})
        submitted=time.time()
        # POST is attempted exactly once: lost acknowledgement is not safe to retry.
        key=self.request('jobs',{'preset':preset,'prompt':prompt,'workers':workers,'policy':policy},status['csrf'])['id']
        save(self.root/label/'submission.json',{'id':key,'preset':preset,'submitted':submitted})
        deadline=time.monotonic()+360
        while time.monotonic()<deadline:
            try:detail=self.request('jobs/'+key)
            except OSError:
                time.sleep(1);continue  # read-only polling may be retried
            if detail['status'] not in ('running','cancelling'):break
            time.sleep(.25)
        else:raise RuntimeError('Existing workflow still active; no repeated paid submission')
        save(self.root/label/'detail.json',detail)
        return detail


def metrics(detail):
    evidence=model_usage(detail)
    events=detail.get('events',[])
    cpu=0.0
    found_cpu=False
    for event in events:
        values=dict(line.split() for line in event.get('usage',{}).get('cpu.stat','').splitlines())
        if 'usage_usec' in values:cpu+=int(values['usage_usec'])/1e6;found_cpu=True
    memory=[h['memory_bytes'] for h in detail.get('history',[]) if h.get('memory_bytes') is not None]
    return {'usage':evidence['totals'] if evidence['calls'] else {},'model_calls':evidence['recorded_model_calls'],
            'usage_complete':evidence['usage_complete'],'cpu_seconds':cpu if found_cpu else None,
            'peak_memory_bytes':max(memory) if memory else None,
            'wall_seconds':detail.get('finished',time.time())-detail['created']}


def result_for(detail,key):
    return next(e['result']['result'] for e in reversed(detail['events'])
                if e['task_id']==key and e['state']=='succeeded')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--max-calls',type=int,default=100)
    args=parser.parse_args()
    if not 1<=args.repeats<=3 or not 8+30*args.repeats<=args.max_calls<=100:
        parser.error('Budget must cover requested design, with maximum 100 calls')
    root=args.output_dir.resolve();root.mkdir(parents=True,exist_ok=True)
    os.chdir(Path(__file__).resolve().parents[1])
    attempts=delegated_attempts()
    console=Console(root,args.max_calls)
    records=[]
    nonce=secrets.token_hex(4)
    prompt=('处理需求：设计一个小型Python整数金额计算服务的测试方案。'
            '每个Agent只输出JSON，形如{"tag":"新生成的8位ASCII字母数字标记",'
            '"received_tags":["逐字复制upstream_results中直接前驱JSON的tag"],'
            '"answer":"80字以内的本角色结论"}。每个Agent自行生成不同tag，入口received_tags为空；'
            '不要提前猜其他Agent的标记。汇总Agent应整合实际收到的分析和测试结论。运行编号:'+nonce)
    team=console.submit('collaboration-team','team',prompt)
    tag_check=check_tag_flow(team)
    save(root/'collaboration-team/evidence.json',tag_check)
    review_prompt=('给出整数金额服务的测试方案，最终须含折扣边界、免邮边界、退款舍入三个具体测试。'
        '这是受控修订试验：若未收到上轮反馈，先明确列出这三个验收条件，但故意只写一条测试，并说明草稿尚缺另外两条；'
        '若已收到评审反馈，则补全三条，保留验收条件并逐项回应意见。答复须简短，不超过180字。')
    review=console.submit('collaboration-review','review',review_prompt)
    review_check=check_review(review)
    save(root/'collaboration-review/evidence.json',review_check)
    for label,detail in [('collaboration-team',team),('collaboration-review',review)]:
        data=metrics(detail)
        records.append({'id':label,'kind':'live','scenario':label,'workers':2,'policy':'resource_fit',
                        'cache_mode':'lru2','model':'deepseek-flash',
                        'success':detail['status']=='succeeded' and (tag_check if label=='collaboration-team' else review_check)['passed'],
                        'usage_source':'provider_usage','billed_model_calls':data['model_calls'],**data})
    baseline=run_check(BASELINE_SOURCE,root/'baseline')
    save(root/'baseline.json',baseline)
    if baseline['success'] or baseline['baseline_failing_count']==0:raise RuntimeError('Original project is not red')
    best=None
    configurations=[(1,'fifo'),(2,'aging_fit'),(1,'resource_fit'),(2,'fifo'),(1,'aging_fit'),(2,'resource_fit')]
    consecutive_errors=0
    for repeat in range(args.repeats):
        order=configurations[repeat:]+configurations[:repeat]
        if repeat%2:order=list(reversed(order))
        for workers,policy in order:
            label=f'project-r{repeat}-w{workers}-{policy}'
            directory=root/label;directory.mkdir(parents=True,exist_ok=True)
            started=time.monotonic()
            specification=project_context()
            project_prompt=(specification+'\nThis is a real code repair. Planner/analyst/tester: short diagnosis/test reasoning only. '
                'Final reviewer: return ONLY one JSON object {"source":"complete corrected pricing.py"}, '
                'no Markdown, no explanation. Preserve all three functions and invalid-input checks. '
                'Use integer // flooring; no int(), round(), imports or unsupported syntax. Keep code compact.')
            detail=console.submit(label+'/team','team',project_prompt,workers,policy)
            raw=None;check=None;validation_events=[];review_detail=None;accepted=False;error=None
            try:
                raw=result_for(detail,'review')
                source=validate_patch(raw)
                save(directory/'model-patch.json',json.loads(raw))
                tool=[{'id':'test_patch','action':'command','timeout':15,
                       'resources':{'cpu_milli':1000,'memory_bytes':192*1024**2,'pids':16},
                       'argv':[sys.executable,'-I','-S',str(Path(__file__).resolve().parents[1]/'agent_runtime/engineering.py'),
                               'check','--response',str(directory/'model-patch.json'),'--workspace',str(directory/'checks')]}]
                _,validation_events=run(tool,output_dir=directory/'tool-runs',cgroup_root=attempts,quiet=True)
                reports=list((directory/'checks').glob('pricing-*/report.json'))
                if reports:check=json.loads(reports[-1].read_text())
                if not check:raise ValueError('Test tool produced no report')
                if check['success'] and best is None:best=(raw,detail)
                audit_prompt=('Review this fixed Python repair. Return ONLY JSON '
                    '{"accepted":true or false,"source_sha256":"copy the supplied hash","reason":"short"}. '
                    'Approve only if requirements are met AND both public/heldout tests pass.\n'+
                    json.dumps({'requirements':json.loads(specification)['requirements'],'source':source,
                        'source_sha256':check['source_sha256'],
                        'public':{k:v for k,v in check['public'].items() if k!='stdout'},
                        'heldout':{k:v for k,v in check['heldout'].items() if k!='stdout'}},separators=(',',':')))
                review_detail=console.submit(label+'/final-review','llm',audit_prompt,workers,policy)
                verdict=json.loads(result_for(review_detail,'assistant'))
                accepted=verdict.get('accepted') is True and verdict.get('source_sha256')==check['source_sha256']
                save(directory/'verdict.json',verdict)
            except (ValueError,KeyError,StopIteration) as exc:
                error=type(exc).__name__+': '+str(exc)[:300]
            data=metrics(detail)
            if review_detail:
                other=metrics(review_detail)
                for key,value in other['usage'].items():data['usage'][key]=data['usage'].get(key,0)+value
                data['model_calls']+=other['model_calls']
                data['cpu_seconds']=(data['cpu_seconds'] or 0)+(other['cpu_seconds'] or 0)
                peaks=[v for v in (data['peak_memory_bytes'],other['peak_memory_bytes']) if v is not None]
                data['peak_memory_bytes']=max(peaks) if peaks else None
            for event in validation_events:
                stats=dict(line.split() for line in event.get('usage',{}).get('cpu.stat','').splitlines())
                if 'usage_usec' in stats:data['cpu_seconds']=(data['cpu_seconds'] or 0)+int(stats['usage_usec'])/1e6
            record={'id':label,'kind':'live','scenario':'integer-pricing','workers':workers,'policy':policy,
                'cache_mode':'lru2','model':'deepseek-flash','usage_source':'provider_usage',**data,
                'billed_model_calls':data['model_calls'] if data['usage_complete'] else None,
                'success':bool(check and check['success'] and accepted),
                'wall_seconds':time.monotonic()-started,'source_sha256':check['source_sha256'] if check else None,
                'test_results':{'public':check['public'],'heldout':check['heldout'],'baseline':check['baseline']} if check else None,
                'runtime_budget':detail.get('budget'),'accepted':accepted,'error':error,'repeat':repeat}
            records.append(record)
            save(root/'records.json',records)
            print(json.dumps({k:record[k] for k in ('id','success','wall_seconds','model_calls','accepted','error')}),flush=True)
            consecutive_errors=consecutive_errors+1 if error else 0
            if consecutive_errors>=2:
                raise RuntimeError('Two consecutive pipeline errors; stopping paid calls for diagnosis')
    if best:
        raw,detail=best
        contexts=[json.loads(project_context()),{'upstream_model_results':{
            e['task_id']:e['result']['result'] for e in detail['events'] if e['state']=='succeeded' and e.get('result')}}]
        replayed=replay(contexts,raw,root/'cache-replay',rounds=8,repeats=3)
        for index,row in enumerate(replayed['records']):
            records.append({**row,'id':f'cache-replay-{index}','kind':'replay','scenario':'integer-pricing',
                'workers':1,'policy':'fixed_trace','model':'deepseek-flash','usage_source':'fixed_output_replay'})
    save(root/'records.json',records)
    save(root/'rates.json',{'currency':'CNY','model':'deepseek-flash','input_per_million':2,
                          'cached_input_per_million':.04,'output_per_million':8})
    save(root/'methodology.json',{'live_project_configurations':6,'repeats':args.repeats,
        'live_call_upper_bound':console.reserved,'max_calls':args.max_calls,
        'rates_source':'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/',
        'rates_basis':'peak rates conservative estimate; actual bill unavailable',
        'baseline_source_never_contains_reference_repair':True,
        'cpu_scope':'local Agent/tool Workers only; not provider compute',
        'concurrency_confound':'workers=1 budget=1CPU; workers=2 budget=2CPU; not pure parallelism',
        'remote_cache_and_model_variation':'not controlled; all workflow prompts fixed after initial protocol check',
        'cache_comparison':'same real output replay plus real tests, zero new inference',
        'not_a_general_python_sandbox':True})
    print('EXPERIMENTS_COMPLETE',len(records),flush=True)


if __name__=='__main__':main()
