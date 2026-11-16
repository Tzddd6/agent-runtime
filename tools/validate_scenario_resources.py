"""Strict resource/fault checks, no supplier calls. Use non-root Delegate=yes."""
import ast
import json
import os
from pathlib import Path
import subprocess
import re,time,statistics
import sys
root=Path(__file__).resolve().parents[1]
os.chdir(root)
if os.name!='posix' or os.getuid()==0:raise SystemExit('Use non-root delegated service')
relative=Path('/proc/self/cgroup').read_text().strip().split('0::',1)[1].splitlines()[0]
group=Path('/sys/fs/cgroup')/relative.lstrip('/')
(group/'control').mkdir();(group/'control/cgroup.procs').write_text(str(os.getpid()))
(group/'cgroup.subtree_control').write_text('+cpu +memory +pids')
attempts=group/'attempts';attempts.mkdir();(attempts/'cgroup.subtree_control').write_text('+cpu +memory +pids')
names=[]
for node in ast.parse((root/'tools/validate_linux.py').read_text()).body:
    if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='strict_names' for t in node.targets):names+=ast.literal_eval(node.value)
    elif isinstance(node,ast.AugAssign) and isinstance(node.target,ast.Name) and node.target.id=='strict_names':names+=ast.literal_eval(node.value)
names=[name for name in names if not name.startswith('test_agents.')]
names+=['test_agents','test_failure_scenarios','test_artifact_binding','test_v18_execution','test_unified_execution','test_local_repair','test_runtime_capabilities','test_goal_history']
result=subprocess.run([sys.executable,'-m','unittest','-v',*names],env={**os.environ,'AGENTRT_TEST_CGROUP':str(attempts)},stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
print(result.stdout,flush=True)
remaining=[p.name for p in attempts.iterdir() if p.is_dir()]
found=re.search(r'Ran (\d+) tests?',result.stdout)
report={'tests_ran':int(found[1]) if found else None,'selected':names,
        'passed':result.returncode==0 and not remaining,'remaining_attempt_cgroups':remaining,
        'resource_mode':'cgroup_v2','supplier_calls':0}
(root/'validation/core-2026-11-16').mkdir(parents=True,exist_ok=True)
(root/'validation/core-2026-11-16/resource-check.json').write_text(json.dumps(report,indent=2))
print('RESOURCE_CHECK '+json.dumps(report),flush=True)
if not report['passed']:raise SystemExit(1)
sys.path.insert(0,str(root))
from agent_runtime.runtime import run
policies=['fifo','resource_fit','aging_fit','drf'];rows=[]
for repeat in range(3):
    ordered=policies[repeat:]+policies[:repeat]
    for policy in ordered:
        tasks=[{'id':'anchor','action':'sleep','seconds':.35,'resources':{'cpu_milli':1000,'memory_bytes':64*1024**2,'pids':16}},
               {'id':'wide','action':'sleep','seconds':.12,'resources':{'cpu_milli':2000,'memory_bytes':64*1024**2,'pids':16}},
               {'id':'small','action':'sleep','seconds':.08,'resources':{'cpu_milli':500,'memory_bytes':64*1024**2,'pids':16}}]
        snapshots=[];start=time.monotonic()
        states,events=run(tasks,quiet=True,max_workers=3,cgroup_root=str(attempts),policy=policy,
            budget={'cpu_milli':2000,'memory_bytes':1024**3,'pids':128},output_dir=root/'reports/four-policy-runs',observer=snapshots.append)
        assert all(value=='succeeded' for value in states.values())
        assert all(0<=sample['reserved']['cpu_milli']<=2000 and sample['reserved']['memory_bytes']<=1024**3 for sample in snapshots)
        assert not any(path.is_dir() for path in attempts.iterdir())
        rows.append({'repeat':repeat,'policy':policy,'seconds':time.monotonic()-start,
            'small_wait_seconds':next(e['time'] for e in events if e['task_id']=='small' and e['state']=='running')-events[0]['time'],
            'passed':True,'reservation_within_budget':True,'remaining_attempt_cgroups':0})
policies_report={'rows':rows,'medians':{policy:{key:statistics.median(row[key] for row in rows if row['policy']==policy) for key in ('seconds','small_wait_seconds')} for policy in policies},
    'limits':'finite head-of-line sleep fixture, not universal strategy superiority or model generation preemption'}
(root/'validation/core-2026-11-16/four-policies.json').write_text(json.dumps(policies_report,indent=2))
print('FOUR_POLICIES '+json.dumps(policies_report['medians']),flush=True)
