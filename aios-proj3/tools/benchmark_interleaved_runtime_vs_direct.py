"""Fair cold end-to-end comparison of Runtime and direct orchestration.

Both arms use identical engineering protocol, isolated ProcessService tools,
model parameters and up to two patch rounds. Default: fixed responses, no API.
Live calls require explicit --live; never infer permission from an env key.
This measures the selected integration, not an official benchmark or ranking.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import os
import statistics
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from aios.tool import engineering
from aios.os_kernel.process_service import ProcessService
from aios.scheduler.drf_admission import DRFAdmissionController, ResourceVector
from cerebrum.llm.apis import LLMResponse
from runtime.engineering_protocol import analysis_plan, patch_messages, decode_patch
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole

BENCH_PROJECTS = ('order_service', 'pricing', 'intervals', 'text_normalization')


def _provider_call(messages, project, use_stub=True, api_key='', model='deepseek-chat', *, bad_patch=False):
    if use_stub:
        spec = engineering.PROJECTS[project]
        if spec.get('multi_file'):
            text = json.dumps({'files': spec['files'] if bad_patch else spec['reference_fix_files']}, ensure_ascii=False)
        else:
            text = json.dumps({'source': spec['source'] if bad_patch else spec['reference_fix']}, ensure_ascii=False)
        return text, len(json.dumps(messages, ensure_ascii=False))//4, len(text)//4
    if bad_patch:
        raise ValueError('Fault-response injection is offline only')
    body = {'model': model, 'messages': messages, 'max_tokens': 4096, 'temperature': 0.0}
    req = urllib.request.Request('https://api.deepseek.com/v1/chat/completions',
        data=json.dumps(body).encode(), headers={'Authorization':'Bearer '+api_key,
        'Content-Type':'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=60) as response:
        raw=json.load(response)
    choice=(raw.get('choices') or [{}])[0]
    if choice.get('finish_reason') != 'stop':
        raise RuntimeError('Incomplete provider response; no automatic replay')
    usage=raw.get('usage') or {}
    # Missing real usage remains unknown, never relabeled character estimates.
    return choice['message']['content'], usage.get('prompt_tokens'), usage.get('completion_tokens')


def _direct_llm_call(messages, project, use_stub=True, api_key='', model='deepseek-chat', **kwargs):
    return _provider_call(messages, project, use_stub, api_key, model, **kwargs)


def _record_call(records, messages, callback):
    record={'input_sha256':sha256(json.dumps(messages,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
            'temperature':0.0,'max_tokens':4096,'status':'failed'}
    records.append(record)
    started=time.perf_counter()
    try:
        text, inp, out=callback()
        record.update(status='succeeded',input_tokens=inp,output_tokens=out)
        return text, inp, out
    except Exception as exc:
        record['error_type']=type(exc).__name__
        raise
    finally:
        record['seconds']=time.perf_counter()-started


def _run_direct_loop_single_project(project, use_stub=True, api_key='', inject_audit_fault=False,
                                     *, service=None, fail_first=False, records=None):
    if inject_audit_fault:
        raise ValueError('Audit faults are not compared without symmetric injection')
    if service is None:
        raise ValueError('A shared isolated ProcessService is required')
    spec=engineering.manifest(project, include_reference=False)
    multi=bool(spec.get('multi_file'));records=[] if records is None else records
    def check(operation, params):
        return service.run('tester_agent',operation,params)
    previous=check('project.test',{'project':project})
    if previous['success']:
        raise RuntimeError('Baseline did not fail')
    plan=analysis_plan(project,spec,previous)
    for round_no in range(2):
        messages=patch_messages(project,spec,{'plan':plan} if round_no==0 else previous,retry=round_no>0)
        text, _, _=_record_call(records,messages,lambda: _direct_llm_call(messages,project,use_stub,api_key,
                                                              bad_patch=fail_first and round_no==0))
        candidate=decode_patch(text,multi_file=multi)
        previous=check('project.patch_check',{'project':project,**candidate})
        if previous['success']:
            break
    if not previous['success']:
        raise RuntimeError('Candidate failed independent tests')
    blob,_=engineering.build_delivery_zip_bytes(previous,'direct_'+uuid4().hex)
    engineering.verify_delivery_zip_bytes(blob,previous['source_sha256'],multi_file=multi)
    return _project_row(project,previous,records)


def _project_row(project, checked, records):
    return {'project':project,'success':bool(checked.get('success')),
        'public_passed':checked['public']['passed'],'public_total':checked['public']['total'],
        'heldout_passed':checked['heldout']['passed'],'heldout_total':checked['heldout']['total'],
        'calls':records}


def run_one_arm_round(arm, round_idx, concurrency=1, use_stub=True, api_key='', *, fail_first=False):
    if concurrency != 1:
        raise ValueError('Only actual concurrency=1 is implemented')
    if arm not in ('A','B'):
        raise ValueError('Invalid arm')
    start=time.perf_counter();cpu=time.process_time();rows=[];error=None;receipts=[];records=[]
    with TemporaryDirectory(prefix='aios_fair_') as folder:
        if arm=='A':
            with AIOSKernelRuntime(storage_root=str(Path(folder)/'store'),log_mode='silent') as kernel:
                console=NativeConsole(kernel,Path(folder)/'jobs')
                try:
                    for project in BENCH_PROJECTS:
                        records=[]
                        def model(query):
                            text,inp,out=_record_call(records,query.messages,lambda:_provider_call(query.messages,
                                project,use_stub,api_key,bad_patch=fail_first and len(records)==1))
                            usage={} if inp is None or out is None else {'prompt_tokens':inp,'completion_tokens':out}
                            return LLMResponse(response_message=text,usage=usage)
                        console.model_service.complete=model
                        turn=console.conversations.submit({'conversation_id':uuid4().hex,'request_id':uuid4().hex,
                            'mode':'engineering','project':project,'prompt':'Repair '+project,
                            'use_memory':False,'use_knowledge':False,'allow_repair_round_2':True,
                            'model_budget':{'calls':2,'output_tokens':8192}})
                        if turn['status']!='succeeded':
                            raise RuntimeError('Runtime verification failed')
                        # Both arms build and read back a verified delivery archive.
                        console.download_zip(turn['job_id'])
                        rows.append(_project_row(project,json.loads(turn['answer']),records))
                    receipts=kernel.process_service.snapshot()['receipts']
                except Exception as exc:
                    error=type(exc).__name__
                    rows.append({'project':project,'success':False,'error_type':error,'calls':records})
                    receipts=kernel.process_service.snapshot()['receipts']
                finally:
                    console.close()
        else:
            service=ProcessService(Path(folder)/'workers')
            service.admission=DRFAdmissionController()
            service.admission.register_agent('tester_agent',resource_limit=ResourceVector(500,128*1024*1024,8))
            try:
                for project in BENCH_PROJECTS:
                    records=[]
                    rows.append(_run_direct_loop_single_project(project,use_stub,api_key,service=service,
                                                               fail_first=fail_first,records=records))
                receipts=service.snapshot()['receipts']
            except Exception as exc:
                error=type(exc).__name__
                rows.append({'project':project,'success':False,'error_type':error,'calls':records})
                receipts=service.snapshot()['receipts']
            finally:
                service.close()
    calls=[c for row in rows for c in row['calls']]
    return {'arm':'AIOS_RUNTIME' if arm=='A' else 'DIRECT_LOOP','arm_code':arm,'round':round_idx,
        'status':'failed' if error else 'succeeded','error_type':error,
        'elapsed_ms':(time.perf_counter()-start)*1000,'coordinator_cpu_seconds':time.process_time()-cpu,
        'projects_passed':sum(r['success'] for r in rows),'projects_total':len(BENCH_PROJECTS),
        'repair_success_rate':sum(r['success'] for r in rows)/len(BENCH_PROJECTS),
        'model_calls':len(calls),'input_tokens':sum(c['input_tokens'] for c in calls) if all(c.get('input_tokens') is not None for c in calls) else None,
        'output_tokens':sum(c['output_tokens'] for c in calls) if all(c.get('output_tokens') is not None for c in calls) else None,
        'token_metric_kind':'estimated_char_div_4_stub' if use_stub else 'provider_usage_or_unknown',
        'peak_rss_kib':None,'resource_note':'No per-arm total RSS attribution. Worker receipts retained.',
        'worker_receipts':receipts,'projects':rows}


def build_interleaved_schedule(rounds):
    return [(index,arm) for index in range(1,rounds+1) for arm in (('A','B') if index%2 else ('B','A'))]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stub-rounds',type=int,default=3)
    parser.add_argument('--real-rounds',type=int,default=0)
    parser.add_argument('--concurrency',type=int,choices=[1],default=1)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--output',type=Path,default=PROJECT_ROOT/'reports/fair-interleaved.json')
    args=parser.parse_args()
    if not 1<=args.stub_rounds<=10:parser.error('stub-rounds must be 1..10')
    if args.live or args.real_rounds:
        parser.error('This handoff validates offline only; live runs require the shared cost-ledger adapter, not a raw env-key CLI')
    runs=[]
    for index,arm in build_interleaved_schedule(args.stub_rounds):
        runs.append(run_one_arm_round(arm,index))
    equal=[]
    for index in range(1,args.stub_rounds+1):
        pair=[row for row in runs if row['round']==index]
        hashes=[[c['input_sha256'] for p in row['projects'] for c in p['calls']] for row in pair]
        equal.append(all(row['status']=='succeeded' for row in pair) and hashes[0]==hashes[1])
    summary={arm:{'successes':sum(r['status']=='succeeded' for r in runs if r['arm_code']==arm),
        'median_ms':statistics.median(r['elapsed_ms'] for r in runs if r['arm_code']==arm)} for arm in ('A','B')}
    report={'scope':'cold end-to-end, same isolated tool executor; no total memory advantage claimed',
        'fixed_conditions':{'concurrency':1,'temperature':0.0,'max_output_tokens':4096,'max_patch_rounds':2},
        'same_prompt_hash_by_pair':equal,'summary':summary,'runs':runs,
        'stage2_real_deepseek':{'status':'not_run','reason':'No paid calls; integrate the shared cost ledger before live CLI'}}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('same_prompt_hash_by_pair','summary')}),flush=True)
    return 0 if all(equal) else 1


if __name__=='__main__':
    raise SystemExit(main())
