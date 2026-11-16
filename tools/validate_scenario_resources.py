"""Strict resource/fault checks, no supplier calls. Use non-root Delegate=yes."""
import ast
import json
import os
from pathlib import Path
import subprocess
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
names+=['test_failure_scenarios','test_artifact_binding']
result=subprocess.run([sys.executable,'-m','unittest','-v',*names],env={**os.environ,'AGENTRT_TEST_CGROUP':str(attempts)})
remaining=[p.name for p in attempts.iterdir() if p.is_dir()]
report={'strict_existing_methods':len(names)-2,'new_test_modules':['test_failure_scenarios','test_artifact_binding'],
        'passed':result.returncode==0 and not remaining,'remaining_attempt_cgroups':remaining,
        'resource_mode':'cgroup_v2','supplier_calls':0}
(root/'validation/scenarios-2026-11-16/resource-check.json').write_text(json.dumps(report,indent=2))
print('RESOURCE_CHECK '+json.dumps(report),flush=True)
raise SystemExit(0 if report['passed'] else 1)
