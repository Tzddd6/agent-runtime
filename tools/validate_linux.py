import contextlib, io, json, os, re, subprocess, sys, time
from pathlib import Path
project=Path(__file__).resolve().parents[1]
if sys.platform != 'linux' or os.geteuid() == 0:
 raise SystemExit('需要在 Linux 上通过 User=普通用户、Delegate=yes 的临时服务运行')
os.chdir(project)
sys.path.insert(0,str(project))
print('PLATFORM',sys.version,flush=True)
ret=subprocess.run([sys.executable,'-m','unittest','-v'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
print(ret.stdout,flush=True)
regular_tests=int(re.search(r'Ran (\d+) tests?',ret.stdout).group(1))
skipped=re.search(r'OK \(skipped=(\d+)\)',ret.stdout)
regular_skipped=int(skipped.group(1)) if skipped else 0
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
strict_names=[
 'test_execution_speed.ExecutionSpeedTests.test_dependency_chain_dispatches_after_cleanup_and_records_scheduler_stats',
 'test_chat.ChatTests.test_real_document_chat_restores_and_repeated_post_does_not_execute_again',
 'test_model_budget.ModelBudgetTests.test_runtime_cross_task_agent_budget_does_not_block_independent_agent',
 'test_model_budget.ModelBudgetTests.test_dynamic_agent_identity_cannot_bypass_workflow_budget',
 'test_paper_mechanisms.PaperMechanismTests.test_real_workers_receive_refs_and_materialize_identical_inputs',
 'test_agents.AgentTests.test_linux_aggregate_agent_cgroup_and_cleanup',
 'test_extended.ExtendedTests.test_linux_cgroup_oom',
 'test_features.FeatureTests.test_linux_pids_limit',
 'test_features.FeatureTests.test_linux_detached_descendant_cleanup',
 'test_features.FeatureTests.test_linux_timeout_cleanup_and_retry',
 'test_telemetry.TelemetryTests.test_linux_real_measurements',
 'test_executor_lifecycle.ExecutorLifecycleTests.test_linux_tiny_cpu_budget_large_payload_still_has_a_deadline',
 'test_context_control.ContextControlTests.test_real_workers_share_semantic_facts_only_after_explicit_delegation',
 'test_context_control.ContextControlTests.test_forged_delegation_fails_without_stopping_independent_agent',
 'test_context_control.ContextControlTests.test_real_model_worker_uses_bounded_view_and_preserves_evidence',
 'test_context_control.ContextControlTests.test_empty_claim_contract_is_isolated_before_publication']
ret=subprocess.run([sys.executable,'-m','unittest','-v',*strict_names],env=env)
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
report={'states':states,'wall_seconds':elapsed,'cpu_core_ratio':ratio,'cpu_stat':usage,'oom_test':'passed','enforcement':'cgroup_v2','runtime_user':os.getuid(),
        'regular_discovered':regular_tests,'regular_passed':regular_tests-regular_skipped,
        'regular_skipped':regular_skipped,'strict_tests_passed':len(strict_names)}
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
for command in ([sys.executable,str(project/'tools/benchmark_context.py'),'--rounds','8','--repeats','3'],
                [sys.executable,str(project/'tools/benchmark_fairness.py'),'--repeats','3','--cgroup-root',str(attempts)]):
 ret=subprocess.run(command)
 if ret.returncode:raise SystemExit(ret.returncode)
assert not [p for p in attempts.iterdir() if p.is_dir()]
report.update(document_pipeline=doc_states,ipc=ipc_states,pids_test='passed',
              descendant_cleanup='passed',timeout_retry_cleanup='passed',
              live_measurements='passed',
              platform=os.uname().release,python=sys.version,
              context_stats=json.loads((project/'runs'/doc_events[0]['workflow_id']/'summary.json').read_text())['context_stats'])
(project/'runs/linux-validation-latest.json').write_text(json.dumps(report,indent=2))
(project/'reports').mkdir(exist_ok=True)
(project/'reports/linux-validation-latest.json').write_text(json.dumps(report,indent=2))
print('LINUX_VALIDATION',json.dumps(report),flush=True)
