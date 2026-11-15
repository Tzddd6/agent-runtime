"""Bounded real-provider benchmark; emits numeric metadata, never credentials or text.

Run inside a dedicated delegated service with the existing broker EnvironmentFile.
Each sample submits exactly four paid calls; no application retry is enabled.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import resource
import re


def child(repo):
    os.chdir(repo)
    sys.path.insert(0,str(Path(repo).resolve()))
    from agent_runtime.model_broker import BrokerClient
    from agent_runtime.model_gateway import ModelGateway
    from agent_runtime.runtime import run
    from agent_runtime.scenarios import team_tasks
    from unittest.mock import patch
    records=[];lock=threading.Lock();known_outputs=set()
    class MeasuredClient(BrokerClient):
        def _request(self,path,body=None):
            value=super()._request(path,body)
            if path.startswith('/calls/') and body is None:
                local=getattr(self,'measurement',None)
                if local is not None:
                    local['polls']+=1
                    if value['status']=='running' and 'running_seen_after_seconds' not in local:
                        local['running_seen_after_seconds']=time.monotonic()-local['_started']
                    if value['status']=='succeeded':
                        local['broker_elapsed_seconds']=value['finished']-value['created']
                        local['completion_detection_seconds']=max(0,time.time()-value['finished'])
            return value
        def complete(self,messages,max_tokens=256):
            self.measurement={'_started':time.monotonic(),'polls':0,
                'request_bytes':len(json.dumps(messages,ensure_ascii=False).encode()),
                'max_tokens':max_tokens,
                'upstream_input_count':0}
            for msg in messages:
                try:content=json.loads(msg['content'])
                except (ValueError,TypeError):continue
                if isinstance(content,dict):
                    upstream=content.get('upstream_results',{})
                    self.measurement['upstream_input_count']=len(upstream)
                    self.measurement['upstream_content_present']=all(bool(v) for v in upstream.values())
                    with lock:
                        self.measurement['upstream_integrity_verified']=all(
                            isinstance(v,str) and hashlib.sha256(v.encode()).hexdigest() in known_outputs
                            for v in upstream.values())
            result=super().complete(messages,max_tokens)
            record={k:v for k,v in self.measurement.items() if not k.startswith('_')}
            record.update(total_seconds=time.monotonic()-self.measurement['_started'],
                provider_seconds=result.get('latency_seconds'),finish_reason=result.get('finish_reason'),
                usage=result.get('usage',{}))
            if max_tokens==512:
                record['upstream_witnesses_used']=all(t in result['content'] for t in witnesses)
                record['coverage']={k:any(word in result['content'] for word in words) for k,words in
                    {'empty_input':('空','empty'),'duplicates':('重复','duplicate'),
                     'recovery':('恢复','异常','失败'),'concurrency':('并发','锁'),
                     'resources':('资源','内存','CPU','限额')}.items()}
            with lock:
                known_outputs.add(hashlib.sha256(result['content'].encode()).hexdigest());records.append(record)
            return result
    client=MeasuredClient.from_env()
    # from_env is a classmethod and preserves the measured subclass.
    prompt=('为三人大学生团队设计一个小型 Python 待办事项项目的分析、测试与修复流程。'
            '覆盖空输入、重复项、异常恢复、并发和资源限制，指出验收条件。')
    tasks=team_tasks(prompt)
    witnesses=['WITNESS_A_'+uuid.uuid4().hex[:6],'WITNESS_T_'+uuid.uuid4().hex[:6]]
    for key,witness in zip(('analyze','test_plan'),witnesses):
        next(t for t in tasks if t['id']==key)['messages'][0]['content']+=' 首行写见证标记 '+witness+'。'
    tasks[-1]['messages'][0]['content']+=' 首行原样保留两个上游结果中 WITNESS_ 开头的见证标记。'
    with tempfile.TemporaryDirectory(prefix='agentrt-real-speed-') as tmp:
        start=time.monotonic();cpu=os.times();peak_worker_memory=[0];peak_reserved={'cpu_milli':0,'memory_bytes':0}
        def observe(sample):
            memory=sum(t.get('metrics',{}).get('memory_bytes') or 0 for t in sample.get('tasks',[]) if t['state']=='running')
            peak_worker_memory[0]=max(peak_worker_memory[0],memory)
            for key in peak_reserved:peak_reserved[key]=max(peak_reserved[key],sample.get('reserved',{}).get(key,0))
        with patch.object(ModelGateway,'from_env',side_effect=lambda path:ModelGateway(client,path)):
            states,events=run(tasks,max_workers=2,quiet=True,output_dir=tmp,
                cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'),observer=observe,
                budget={'cpu_milli':2000,'memory_bytes':1024**3,'pids':128},
                model_budget={'calls':4,'output_tokens':1280})
        elapsed=time.monotonic()-start
        success=all(v=='succeeded' for v in states.values())
        intervals={}
        for e in events:
            if e['state']=='running':intervals.setdefault(e['task_id'],{})['start']=e['time']
            elif e['state']=='succeeded':intervals.setdefault(e['task_id'],{})['end']=e['time']
        overlap=0
        if all('end' in intervals.get(k,{}) for k in ('analyze','test_plan')):
            a,b=intervals['analyze'],intervals['test_plan']
            overlap=max(0,min(a['end'],b['end'])-max(a['start'],b['start']))
        output={'seconds':elapsed,'succeeded':success,'paid_calls':len(records),'calls':records,
            'coordinator_cpu_seconds':os.times().user+os.times().system-cpu.user-cpu.system,
            'coordinator_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            'observed_worker_memory_peak_bytes':peak_worker_memory[0],
            'peak_reserved':peak_reserved,
            'worker_cpu_seconds':sum(int(m.group(1))/1e6 for e in events if e['state']=='succeeded'
                for m in [re.search(r'^usage_usec (\d+)$',e.get('usage',{}).get('cpu.stat',''),re.M)] if m),
            'parallel_overlap_seconds':overlap,'task_intervals':intervals,
            'all_upstreams_present':all(r.get('upstream_content_present',True) for r in records),
            'all_upstreams_integrity_verified':all(r.get('upstream_integrity_verified',True) for r in records),
            'upstream_counts':sorted(r['upstream_input_count'] for r in records)}
        if not success:
            output['states']=states
            output['failure_categories']=[{'state':e['state'],'reason':e.get('reason'),'error':e.get('error')}
                for e in events if e['state'] in ('failed','unschedulable','timeout')]
        print(json.dumps(output),flush=True)
        if not success:raise SystemExit(2)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--child');p.add_argument('--baseline')
    p.add_argument('--candidate');p.add_argument('--output');p.add_argument('--repeats',type=int,default=3)
    a=p.parse_args()
    if a.child:
        try:return child(a.child)
        except Exception as exc:
            import traceback
            print(json.dumps({'error_type':type(exc).__name__,
                'locations':[{'file':Path(f.filename).name,'line':f.lineno} for f in traceback.extract_tb(exc.__traceback__)]}),flush=True)
            raise SystemExit(3)
    if not 1<=a.repeats<=3:p.error('repeats must be 1..3 (up to 24 paid calls)')
    rows=[]
    for i in range(a.repeats):
        versions=['baseline','candidate'] if a.candidate else ['baseline']
        if i%2:versions.reverse()
        for version in versions:
            proc=subprocess.run([sys.executable,__file__,'--child',getattr(a,version)],
                capture_output=True,text=True,timeout=210)
            if proc.returncode:
                # Never forward arbitrary provider errors, prompts, or process stderr.
                Path(a.output).write_text(json.dumps({'rows':rows,'aborted':True,'failed_version':version},indent=2))
                try:
                    diagnostic=json.loads(proc.stdout)
                    if 'error_type' in diagnostic:print(json.dumps(diagnostic),flush=True)
                    elif 'states' in diagnostic:print(json.dumps(diagnostic),flush=True)
                except ValueError:pass
                import re
                print(json.dumps({'child_exit':proc.returncode,'stdout_bytes':len(proc.stdout),'stderr_bytes':len(proc.stderr),
                    'error_types':re.findall(r'^([A-Za-z.]+(?:Error|Exception)):',proc.stderr,re.M),
                    'line_numbers':re.findall(r'line (\d+)',proc.stderr)}),flush=True)
                raise SystemExit('Real benchmark stopped after a failed sample; no paid retry')
            row={'version':version,'repeat':i,**json.loads(proc.stdout)};rows.append(row)
            Path(a.output).write_text(json.dumps({'rows':rows},indent=2))
            print(json.dumps({'version':version,'repeat':i,'seconds':row['seconds'],
                'provider_seconds':[r['provider_seconds'] for r in row['calls']],
                'finish_reasons':[r['finish_reason'] for r in row['calls']]}),flush=True)
    summary={}
    for version in sorted(set(r['version'] for r in rows)):
        group=[r for r in rows if r['version']==version]
        summary[version]={'median_seconds':statistics.median(r['seconds'] for r in group),
            'range_seconds':[min(r['seconds'] for r in group),max(r['seconds'] for r in group)],
            'usage_sum':{key:sum(c['usage'].get(key,0) for r in group for c in r['calls'])
                         for key in ('prompt_tokens','completion_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens')},
            'detection_seconds_median':statistics.median(c['completion_detection_seconds'] for r in group for c in r['calls'])}
    Path(a.output).write_text(json.dumps({'rows':rows,'summary':summary,'paid_calls':sum(r['paid_calls'] for r in rows),
        'limits':'Small alternating real API sample; network, generation length and provider cache vary.'},indent=2))
    print(json.dumps(summary),flush=True)


if __name__=='__main__':main()
