"""Bounded real-project console benchmark. No supplier key is read by this tool.

54 sequential jobs: 3 projects x 2 worker counts x 3 policies x 3 repeats.
Every POST is attempted once. A lost acknowledgement requires human recovery;
resuming never resubmits a previously attempted paid job.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request

PROJECTS=('pricing','intervals','text_normalization')
POLICIES=('fifo','resource_fit','aging_fit')
BUDGET={'cpu_milli':2000,'memory_bytes':1073741824,'pids':128}
ACTIVE=('running','cancelling','queued')
TERMINAL=('succeeded','failed','cancelled')
MAX_CALLS=216


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def schedule():
    """Rotate projects/policies and alternate worker order between blocks/repeats."""
    result=[]
    for repeat in range(3):
        for block in range(6):
            for position in range(3):
                project=PROJECTS[(position+block+repeat)%3]
                pair=(block+repeat+PROJECTS.index(project))%6
                if repeat%2:pair=5-pair
                policy=POLICIES[pair//2]
                workers=1+pair%2
                result.append({'index':len(result),'repeat':repeat+1,'project':project,
                               'workers':workers,'policy':policy})
    # Each project/configuration appears once per repeat, no accidental imbalance.
    expected={(p,w,s,r) for p in PROJECTS for w in (1,2) for s in POLICIES for r in (1,2,3)}
    actual={(x['project'],x['workers'],x['policy'],x['repeat']) for x in result}
    assert len(result)==len(actual)==54 and actual==expected
    return result


class StopBenchmark(RuntimeError):pass


class Console:
    def __init__(self,token):
        if not token or len(token)<16:raise StopBenchmark('AGENTRT_PROXY_TOKEN missing or invalid')
        self.token=token
        # Do not allow a redirect to forward the private control token elsewhere.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())

    def request(self,path,body=None,csrf=None):
        headers={'X-AgentRT-Proxy-Token':self.token}
        if csrf:headers['X-AgentRT-CSRF']=csrf
        payload=None if body is None else json.dumps(body).encode('utf-8')
        headers['Content-Type']='application/json'
        request=urllib.request.Request('http://127.0.0.1:18880/api/'+path,data=payload,headers=headers)
        with self.opener.open(request,timeout=12) as response:
            raw=response.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:raise StopBenchmark('Console response exceeds evidence limit')
        return json.loads(raw)

    def get(self,path):
        for attempt in range(5):
            try:return self.request(path)
            except urllib.error.HTTPError as exc:
                if exc.code<500:raise StopBenchmark('Console evidence unavailable (HTTP %s)'%exc.code) from None
            except (OSError,ValueError):pass
            time.sleep(min(attempt+1,3))
        raise StopBenchmark('Console GET unavailable; stopped without another submission')

    def available(self):
        status=self.get('status')
        if status.get('recovery_required'):raise StopBenchmark('Console recovery_required')
        if not status.get('model_configured') or not status.get('model_available'):
            raise StopBenchmark('Configured model service unavailable')
        if status.get('enforcement')!='cgroup_v2' or status.get('fixed_budget') is not True:
            raise StopBenchmark('Fixed cgroup v2 budget required')
        if status.get('budget')!=BUDGET:
            raise StopBenchmark('Console budget differs from benchmark protocol')
        return status


def metrics(detail,entry):
    engineering=detail.get('engineering') or {}
    calls=[];usage={};cpu=0.;has_cpu=False
    for event in detail.get('events',[]):
        call=(event.get('result') or {}).get('model_call')
        if call:
            calls.append({'task_id':event.get('task_id'),'model':call.get('model'),
                          'usage':call.get('usage'), 'latency_seconds':call.get('latency_seconds')})
            for field,value in (call.get('usage') or {}).items():
                if type(value) is int and value>=0:usage[field]=usage.get(field,0)+value
        stat=(event.get('usage') or {}).get('cpu.stat','')
        for line in stat.splitlines():
            parts=line.split()
            if len(parts)==2 and parts[0]=='usage_usec':cpu+=int(parts[1])/1e6;has_cpu=True
    memory=[x['memory_bytes'] for x in detail.get('history',[]) if type(x.get('memory_bytes')) is int]
    if len(calls)>4 or engineering.get('model_calls',0)>4:
        raise StopBenchmark('Per-job model-call bound exceeded')
    return {**{k:entry[k] for k in ('index','repeat','project','workers','policy')},
            'id':detail['id'],'kind':'live','scenario':'project_'+entry['project'],
            'status':detail['status'],'success':detail['status']=='succeeded',
            'wall_seconds':detail.get('finished',entry.get('observed_terminal_at',time.time()))-detail['created'],
            'usage':usage,'usage_complete':len(calls)>0 and all(isinstance(x['usage'],dict) and
               all(type(x['usage'].get(k)) is int for k in ('prompt_tokens','completion_tokens')) for x in calls),
            'model_calls':len(calls),'model_call_details':calls,'billed_model_calls':None,
            'usage_source':'provider_response','cpu_seconds':cpu if has_cpu else None,
            'peak_memory_bytes':max(memory) if memory else None,'budget':detail['budget'],
            'source_sha256':engineering.get('source_sha256'),
            'validation':engineering.get('validation'),'verdict':engineering.get('verdict'),
            'actual_fee':None,'detail_path':'runs/%03d/detail.json'%entry['index']}


def run(root,console,job_timeout=600):
    """A kernel-held lock prevents two harnesses from posting the same checkpoint."""
    root.mkdir(parents=True,exist_ok=True)
    with (root/'benchmark.lock').open('a+b') as lock:
        if os.name=='posix':
            import fcntl
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:raise StopBenchmark('Another benchmark process owns this output directory') from None
        else:
            import msvcrt
            lock.write(b'0');lock.flush();lock.seek(0)
            try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:raise StopBenchmark('Another benchmark process owns this output directory') from None
        return _run(root,console,job_timeout)


def _run(root,console,job_timeout=600):
    order=schedule()
    manifest={'schema':1,'schedule':order,'fixed_budget':BUDGET,'maximum_model_calls':MAX_CALLS,
              'maximum_model_calls_per_job':4,'no_post_retries':True,
              'notes':'Sequential interleaved runs. No performance advantage inferred from one sample.'}
    manifest['protocol_sha256']=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
    root.mkdir(parents=True,exist_ok=True)
    manifest_path=root/'manifest.json';checkpoint_path=root/'checkpoint.json'
    if manifest_path.exists():
        if json.loads(manifest_path.read_text(encoding='utf-8'))!=manifest:
            raise StopBenchmark('Existing manifest differs; use a new output directory')
    else:save(manifest_path,manifest)
    checkpoint=json.loads(checkpoint_path.read_text(encoding='utf-8')) if checkpoint_path.exists() else {
        'protocol_sha256':manifest['protocol_sha256'],'reserved_call_upper_bound':0,'runs':{}}
    if checkpoint.get('protocol_sha256')!=manifest['protocol_sha256']:
        raise StopBenchmark('Checkpoint protocol mismatch')
    if checkpoint.get('halt_reason'):raise StopBenchmark('Benchmark halted: '+checkpoint['halt_reason'])
    if (checkpoint.get('reserved_call_upper_bound')!=4*len(checkpoint.get('runs',{}))
            or checkpoint['reserved_call_upper_bound']>MAX_CALLS):
        raise StopBenchmark('Checkpoint call budget is inconsistent')
    def halt(reason):
        checkpoint['halt_reason']=reason;save(checkpoint_path,checkpoint)
        raise StopBenchmark(reason)
    for item in order:
        index=str(item['index']);entry=checkpoint['runs'].get(index)
        detail_path=root/'runs'/('%03d'%item['index'])/'detail.json'
        if entry and entry.get('phase')=='terminal':
            if not detail_path.exists() or not detail_path.with_name('metrics.json').exists():
                raise StopBenchmark('Terminal evidence missing; refusing resubmit')
            continue
        if entry and not entry.get('id'):
            raise StopBenchmark('Unacknowledged POST intent exists; manual recovery required, never resubmit')
        if not entry:
            deadline=time.monotonic()+180
            while True:
                status=console.available()
                if not any(x['status'] in ACTIVE for x in status['jobs']):break
                if time.monotonic()>deadline:raise StopBenchmark('Console busy with another workflow')
                time.sleep(1)
            if checkpoint['reserved_call_upper_bound']+4>MAX_CALLS:raise StopBenchmark('Call budget exhausted')
            entry={**item,'phase':'post_intent','submitted_at':time.time()}
            checkpoint['runs'][index]=entry;checkpoint['reserved_call_upper_bound']+=4
            save(checkpoint_path,checkpoint)  # Durable before the sole POST attempt.
            try:
                reply=console.request('jobs',{'preset':'engineering','project':item['project'],
                    'workers':item['workers'],'policy':item['policy']},status['csrf'])
                job_id=reply['id']
                if not isinstance(job_id,str) or not job_id.isalnum():raise ValueError()
            except Exception:
                raise StopBenchmark('POST acknowledgement uncertain; intent saved, do not resubmit') from None
            entry.update(id=job_id,phase='polling');save(checkpoint_path,checkpoint)
        # A resumed known job is only polled; its paid POST is never repeated.
        deadline=time.monotonic()+job_timeout
        while True:
            console.available()
            detail=console.get('jobs/'+entry['id'])
            if detail.get('budget')!=BUDGET:
                save(detail_path,detail)
                halt('Submitted job violates fixed resource budget; no further runs')
            if detail['status']=='recovery_required':
                save(detail_path,detail);halt('Runtime recovery_required')
            if detail['status'] not in ACTIVE:break
            if time.monotonic()>deadline:raise StopBenchmark('Workflow deadline exceeded; no further submissions')
            time.sleep(.5)
        entry['observed_terminal_at']=time.time()
        save(detail_path,detail)  # Save before console's <=20 history can evict it.
        if detail['status'] not in TERMINAL:raise StopBenchmark('Unknown terminal workflow state')
        record=metrics(detail,entry)
        save(detail_path.with_name('metrics.json'),record)
        entry['phase']='terminal';save(checkpoint_path,checkpoint)
        collected=[json.loads((root/'runs'/('%03d'%x['index'])/'metrics.json').read_text(encoding='utf-8'))
                   for x in order if checkpoint['runs'].get(str(x['index']),{}).get('phase')=='terminal']
        save(root/'records.json',collected)
        print('%02d/54 %s workers=%s policy=%s status=%s calls=%s'%(
            item['index']+1,item['project'],item['workers'],item['policy'],detail['status'],record['model_calls']),flush=True)
        # Infrastructure failures are not ordinary model patch rejection.
        failures=[e for e in detail.get('events',[]) if e.get('state') in ('timeout','unschedulable')]
        if failures or detail['status']=='cancelled':halt('Infrastructure failure/cancellation; benchmark stopped')
        if detail['status']=='failed' and not detail.get('engineering',{}).get('validation'):
            halt('Workflow failed before a validation result; inspect saved evidence')
    records=[json.loads((root/'runs'/('%03d'%x['index'])/'metrics.json').read_text(encoding='utf-8')) for x in order]
    if sum(x['model_calls'] for x in records)>MAX_CALLS:raise StopBenchmark('Observed model-call total exceeded cap')
    save(root/'records.json',records)
    return records


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--plan-only',action='store_true',help='Print design without reading token or making network calls')
    parser.add_argument('--execute',action='store_true',help='Explicitly authorize up to 216 real model calls')
    args=parser.parse_args()
    if args.plan_only:
        print(json.dumps({'schedule':schedule(),'fixed_budget':BUDGET,'max_calls':MAX_CALLS},indent=2));return
    if not args.execute:parser.error('Use --plan-only or explicitly --execute (up to 216 model calls)')
    try:run(args.output_dir.resolve(),Console(os.environ.get('AGENTRT_PROXY_TOKEN','')))
    except StopBenchmark as exc:raise SystemExit(str(exc)) from None


if __name__=='__main__':main()
