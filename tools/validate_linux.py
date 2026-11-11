import contextlib, io, json, os, subprocess, sys, time
from pathlib import Path
project=Path(__file__).resolve().parents[1]
if sys.platform != 'linux' or os.geteuid() == 0:
 raise SystemExit('需要在 Linux 上通过 User=普通用户、Delegate=yes 的临时服务运行')
os.chdir(project)
sys.path.insert(0,str(project))
print('PLATFORM',sys.version,flush=True)
ret=subprocess.run([sys.executable,'-m','unittest','-v'])
if ret.returncode:raise SystemExit(ret.returncode)
relative=Path('/proc/self/cgroup').read_text().strip().split('0::',1)[1].splitlines()[0]
root=Path('/sys/fs/cgroup')/relative.lstrip('/')
print('DELEGATED_ROOT',root,flush=True)
control=root/'control'
control.mkdir(exist_ok=True)
(control/'cgroup.procs').write_text(str(os.getpid()))
(root/'cgroup.subtree_control').write_text('+cpu +memory +pids')
attempts=root/'attempts'
attempts.mkdir(exist_ok=True)
(attempts/'cgroup.subtree_control').write_text('+cpu +memory +pids')
env=dict(os.environ,AGENTRT_TEST_CGROUP=str(attempts))
ret=subprocess.run([sys.executable,'-m','unittest','-v',
 'test_extended.ExtendedTests.test_linux_cgroup_oom',
 'test_features.FeatureTests.test_linux_pids_limit',
 'test_features.FeatureTests.test_linux_detached_descendant_cleanup',
 'test_features.FeatureTests.test_linux_timeout_cleanup_and_retry'],env=env)
if ret.returncode:raise SystemExit(ret.returncode)
from agent_runtime.runtime import run
start=time.monotonic()
with contextlib.redirect_stdout(io.StringIO()):
 states,events=run([{'id':'cpu_smoke','action':'command','timeout':12,'resources':{'cpu_milli':200,'memory_bytes':134217728},'argv':[sys.executable,'-c','import time; end=time.monotonic()+2\nwhile time.monotonic()<end: pass']}],cgroup_root=str(attempts),output_dir=project/'runs')
elapsed=time.monotonic()-start
usage=dict(line.split() for line in events[-1]['usage']['cpu.stat'].splitlines())
ratio=int(usage['usage_usec'])/1e6/elapsed
assert states['cpu_smoke']=='succeeded',states
assert 0 < ratio < 0.40,ratio
assert int(usage['nr_throttled'])>0,usage
assert not list(attempts.iterdir()) or all(not p.is_dir() for p in attempts.iterdir())
report={'states':states,'wall_seconds':elapsed,'cpu_core_ratio':ratio,'cpu_stat':usage,'oom_test':'passed','enforcement':'cgroup_v2','runtime_user':os.getuid()}
from tools.run_document import tasks_for
with contextlib.redirect_stdout(io.StringIO()):
 doc_states,doc_events=run(tasks_for((project/'README.md').read_text(encoding='utf-8')),
                         cgroup_root=str(attempts),output_dir=project/'runs')
 ipc_states,ipc_events=run(json.loads((project/'examples/ipc.json').read_text()),
                         cgroup_root=str(attempts),output_dir=project/'runs')
assert all(s=='succeeded' for s in doc_states.values()),doc_states
assert all(s=='succeeded' for s in ipc_states.values()),ipc_states
assert not [p for p in attempts.iterdir() if p.is_dir()]
ret=subprocess.run([sys.executable,str(project/'tools/benchmark.py'),'--repeats','3',
                    '--cgroup-root',str(attempts)])
if ret.returncode:raise SystemExit(ret.returncode)
assert not [p for p in attempts.iterdir() if p.is_dir()]
report.update(document_pipeline=doc_states,ipc=ipc_states,pids_test='passed',
              descendant_cleanup='passed',timeout_retry_cleanup='passed',
              platform=os.uname().release,python=sys.version,
              context_stats=json.loads((project/'runs'/doc_events[0]['workflow_id']/'summary.json').read_text())['context_stats'])
(project/'runs/linux-validation-latest.json').write_text(json.dumps(report,indent=2))
(project/'reports').mkdir(exist_ok=True)
(project/'reports/linux-validation-latest.json').write_text(json.dumps(report,indent=2))
print('LINUX_VALIDATION',json.dumps(report),flush=True)
