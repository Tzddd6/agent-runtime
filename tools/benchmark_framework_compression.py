"""Real cgroup startup/context ablation, no model calls or external network.

Three sources/backends, rotated order, fresh coordinator and cgroup per sample.
Includes preloader in total CPU/memory. No claim about cloud KV or answer quality.
"""
import hashlib,json,os,random,statistics,subprocess,sys,time
from pathlib import Path


def child(source,scenario,folder,group,backend):
    sys.stdin.read(1)
    os.chdir(source)
    sys.path.insert(0,source)
    from agent_runtime.runtime import run
    from agent_runtime.ipc import Broker
    rpc={'requests':0,'response_bytes':0};dispatch=Broker.dispatch
    def measured(self,token,value):
        result=dispatch(self,token,value);rpc['requests']+=1
        rpc['response_bytes']+=len(json.dumps(result,ensure_ascii=False).encode())
        return result
    Broker.dispatch=measured
    randomizer=random.Random(20261116)
    text=[''.join(randomizer.choices('abcdefghijklmnopqrstuvwxyz0123456789',k=32768)) for _ in range(4)]
    contexts=[{'text':value} for value in text]
    count=1 if scenario=='single' else 12
    tasks=[{'id':f't{i}','agent':f'a{i}','action':'analyze_text' if scenario=='contexts' else 'echo',
            'resources':{'cpu_milli':250,'memory_bytes':64*1024**2,'pids':16},
            'contexts':contexts if scenario=='contexts' else [],
            'depends_on':[f't{i-1}'] if scenario=='chain' and i else []} for i in range(count)]
    options={} if backend=='legacy' else {'worker_backend':backend}
    if Path(source).name=='agent-runtime-v192-20261116':options['context_compression_level']=1
    start=time.monotonic()
    states,events=run(tasks,max_workers=4,quiet=True,output_dir=Path(folder)/'runs',cgroup_root=str(Path(group)/'attempts'),
        budget={'cpu_milli':2000,'memory_bytes':1024**3,'pids':128},**options)
    elapsed=time.monotonic()-start
    summary=json.loads(next((Path(folder)/'runs').glob('*/summary.json')).read_text())
    expected=hashlib.sha256('\n'.join(text).encode()).hexdigest()
    valid=all(value=='succeeded' for value in states.values())
    if scenario=='contexts':valid=valid and all(e['result']['result']['sha256']==expected for e in events if e['state']=='succeeded')
    valid=valid and not any(p.is_dir() for p in (Path(group)/'attempts').iterdir())
    results_bytes=sum(path.stat().st_size for path in (Path(folder)/'runs').glob('*/*/result.json'))
    result={'passed':valid,'seconds':elapsed,'worker_attempts':sum(e['state']=='running' for e in events),
            'result_json_bytes':results_bytes,'rpc':rpc,'execution':summary.get('execution_stats'),
            'context':summary['context_stats'],'remaining_attempt_cgroups':[] if valid else 'inspect failure'}
    (Path(folder)/'result.json').write_text(json.dumps(result,indent=2))
    if not valid:raise SystemExit(2)


def main():
    if '--child' in sys.argv:
        child(*sys.argv[2:]);return
    if os.getuid()==0:raise SystemExit('Use User=tzd Delegate=yes')
    source=Path(__file__).resolve().parents[1];output=source/'reports/framework-compression-20261116';output.mkdir(mode=0o700,exist_ok=False)
    relative=Path('/proc/self/cgroup').read_text().strip().split('0::',1)[1].splitlines()[0]
    root=Path('/sys/fs/cgroup')/relative.lstrip('/')
    (root/'control').mkdir();(root/'control/cgroup.procs').write_text(str(os.getpid()))
    (root/'cgroup.subtree_control').write_text('+cpu +memory +pids')
    arms=[('baseline','/home/tzd/agent-runtime-v191-20261116','forkserver'),('candidate',str(source),'forkserver')]
    rows=[];index=0
    for repeat in range(5):
        for scenario in ('single','chain','fanout','contexts'):
            ordered=arms[repeat%2:]+arms[:repeat%2]
            for label,package,backend in ordered:
                folder=output/f'{index:03d}-{scenario}-{label}';folder.mkdir()
                group=root/f'case-{index:03d}';group.mkdir();control=group/'control';control.mkdir()
                for name,value in {'cpu.max':'200000 100000','memory.max':str(1024**3),'memory.swap.max':'0','pids.max':'128'}.items():(group/name).write_text(value)
                process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--child',package,scenario,str(folder),str(group),backend],
                    stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,env={'PATH':'/usr/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
                (control/'cgroup.procs').write_text(str(process.pid));(group/'cgroup.subtree_control').write_text('+cpu +memory +pids')
                attempts=group/'attempts';attempts.mkdir();(attempts/'cgroup.subtree_control').write_text('+cpu +memory +pids')
                started=time.monotonic();process.stdin.write(b'G');process.stdin.close();process.stdin=None
                _,error=process.communicate(timeout=60)
                total=time.monotonic()-started
                if process.returncode:raise RuntimeError('sample failed: '+error.decode(errors='replace')[-1200:])
                row=json.loads((folder/'result.json').read_text());row.update(repeat=repeat,scenario=scenario,arm=label,sample_total_seconds=total,
                    cpu_usec=int(dict(line.split() for line in (group/'cpu.stat').read_text().splitlines())['usage_usec']),
                    memory_peak_bytes=int((group/'memory.peak').read_text()))
                rows.append(row);(output/'samples.json').write_text(json.dumps(rows,indent=2))
                attempts.rmdir();control.rmdir();group.rmdir()
                print(json.dumps({'i':index,'scenario':scenario,'arm':label,'seconds':round(row['seconds'],4),'rpc':row['rpc']['requests'],'passed':row['passed']}),flush=True)
                index+=1
    summary={}
    for scenario in ('single','chain','fanout','contexts'):
        summary[scenario]={}
        for label,_,_ in arms:
            selected=[row for row in rows if row['scenario']==scenario and row['arm']==label]
            summary[scenario][label]={key:statistics.median(row[key] for row in selected) for key in ('seconds','sample_total_seconds','cpu_usec','memory_peak_bytes','result_json_bytes')}
            summary[scenario][label]['rpc_requests']=statistics.median(row['rpc']['requests'] for row in selected)
    report={'samples':len(rows),'repeats':5,'all_passed':all(row['passed'] for row in rows),'model_calls':0,
            'summary':summary,'limits':['finite synthetic local tasks, not real model latency comparison','memory includes isolated preloader, worker and new coordinator imports; common Python gate startup excluded','all 12 Workers remain separate processes; fewer interpreter execs, not fewer total processes','local Context transport/cache only; no provider KV control']}
    (output/'summary.json').write_text(json.dumps(report,indent=2));print('CORE_ABLATION '+json.dumps(report),flush=True)


if __name__=='__main__':main()
