"""Resume a frozen tau run using official infrastructure-error envelopes.

No completed simulation is replayed. Model/task/scorer configuration is kept.
Task-level provider recovery is bounded and every failed attempt is retained.
"""
import argparse,hashlib,json,os,sys,time,threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def trajectory_digest(rows):
 values=[r.model_dump(mode='json') if hasattr(r,'model_dump') else r for r in rows]
 return hashlib.sha256(json.dumps(values,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def pending_indices(tasks,simulations,trials):
 ids=[t.id for t in tasks];valid={(i,k) for i in range(trials) for k in ids};seen=set()
 for s in simulations:
  pair=(s.trial,s.task_id)
  if pair not in valid or pair in seen:raise ValueError('Invalid or duplicate checkpoint')
  seen.add(pair)
 return [(i,t) for i in range(trials) for t in tasks if (i,t.id) not in seen]

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--original-run',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--official-root',type=Path,required=True);p.add_argument('--budget-ledger',type=Path,required=True);p.add_argument('--failure-diagnosis',type=Path,required=True);p.add_argument('--run',action='store_true');a=p.parse_args()
 if not a.run:raise SystemExit('Explicit --run required; no provider call made')
 from tau2.data_model.simulation import Results,TextRunConfig,TerminationReason
 from tau2.runner.batch import run_single_task
 from tau2.runner.progress import run_with_retry
 from tau2.evaluator.evaluator import EvaluationType
 from agent_runtime.tau_adapter import TauBridge
 from agent_runtime.model_broker import BrokerClient
 from agent_runtime.model_gateway import ModelError
 from loguru import logger
 logger.remove();logger.add(sys.stderr,level='ERROR')
 original=Results.load(a.original_run/'results.json');assert len(original.tasks)==50 and original.info.num_trials==4
 source_hash=trajectory_digest(original.simulations);count=len(original.simulations);assert count==160
 tasks=original.tasks;pending=pending_indices(tasks,original.simulations,4);assert len(pending)==40 and (pending[0][0],pending[0][1].id)==(3,'10')
 protocol=json.loads((a.original_run/'protocol.json').read_text());config=TextRunConfig.model_validate(protocol['config'])
 diagnosis=json.loads(a.failure_diagnosis.read_text());assert len(diagnosis['failed_calls'])==1;recorded=diagnosis['failed_calls'][0]
 assert recorded['actor']=='agent' and recorded['events']==[{'state':'failed','code':'MODEL_UPSTREAM_FAILED'}]
 broker=BrokerClient.from_env();status=broker.status();assert status['configured'] and status['model']=='deepseek-flash' and status['busy']==0
 manifest=json.loads((a.official_root/'SOURCE_MANIFEST.json').read_text());assert manifest['commit']=='fc0055dc4e0a316c3f83133267fbd6faaa770992'
 verified=0
 for name,digest in manifest['sha256'].items():
  if name.startswith(('src/','data/tau2/domains/','data/tau2/user_simulator/')):
   assert hashlib.sha256((a.official_root/name).read_bytes()).hexdigest()==digest;verified+=1
 a.output.mkdir(mode=0o700,parents=True,exist_ok=False);(a.output/'infrastructure-errors').mkdir(mode=0o700)
 results=original.model_copy(deep=True);results.save(a.output/'results.json');failures=[]
 def persist_error(sim,origin):
  failures.append({'origin':origin,'task_id':sim.task_id,'trial':sim.trial,'seed':sim.seed,'id':sim.id,'termination_reason':sim.termination_reason.value})
  (a.output/'infrastructure-errors'/(sim.id+'.json')).write_text(sim.model_dump_json(indent=2))
  (a.output/'infrastructure-summary.json').write_text(json.dumps(failures,indent=2))
 def recorded_failure():raise RuntimeError('Recorded upstream model call failed; original uncertain request is not reissued')
 envelope=run_with_retry(recorded_failure,tasks[10],3,303,max_retries=0,console_display=False)
 envelope.info.update(recorded_failure_call_id=recorded['id'],recorded_failure_seconds=recorded['seconds'],reconstructed_error_envelope_only=True,conversation_unavailable=True)
 persist_error(envelope,'original_call_failure')
 class TraceClient:
  model=broker.model;timeout=broker.timeout
  def __init__(self):self.failures=[];self.actor='unknown';self.lock=threading.Lock();self.sequence=0
  def write(self,row):
   with self.lock:
    with (a.output/'private-inference-checkpoints.jsonl').open('a',encoding='utf-8') as f:
     os.chmod(f.name,0o600);f.write(json.dumps(row,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
  def complete(self,messages,max_tokens):
   from agent_runtime.benchmark_transport import PREFIX
   assert len(messages)==1 and messages[0]['content'].startswith(PREFIX)
   with self.lock:self.sequence+=1;seq=self.sequence
   self.write({'seq':seq,'stage':'intent','actor':self.actor,'messages':messages,'max_tokens':max_tokens})
   try:
    result=broker.complete(messages,max_tokens)
    self.write({'seq':seq,'stage':'completed','result':{k:result[k] for k in ('content','usage','finish_reason','model') if k in result}});return result
   except ModelError as exc:
    code=exc.code if exc.code in ('MODEL_TIMEOUT','MODEL_UPSTREAM_FAILED','MODEL_TRANSPORT_FAILED') else 'MODEL_UPSTREAM_FAILED'
    self.failures.append(code);self.write({'seq':seq,'stage':'failed','code':code});raise
 client=TraceClient();bridge=TauBridge(client,a.output/'new-attempts',arm='runtime',cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'),cny_limit=20,max_calls=6000,ledger=a.budget_ledger,reuse_workers=True)
 generate=bridge.generate
 def dispatch(*args,**kwargs):
  client.actor='simulator' if kwargs.get('call_name')=='user_simulator_response' else 'agent'
  return generate(*args,**kwargs)
 import tau2.agent.llm_agent as agent
 import tau2.user.user_simulator as user
 agent.generate=dispatch;user.generate=dispatch
 recovery_protocol={'frozen_runtime_source':'ada18ba942fbc051761742fe6dc0471232cb28cb','original_run_completed':count,'original_prefix_sha256':source_hash,'source_official_files_verified':verified,'seed_base':300,'task_config_unchanged':True,'completed_simulations_replayed':0,'original_call_failure_preserved':True,'original_uncertain_call_reissued':False,'recovery':'Fresh official simulation for interrupted task, then untouched remaining tasks; at most one further provider-error task recovery per pending task. Business reward0 never retried.','no_raw_http_retry':True,'authorized_cny_limit':20,'default_config':protocol['config']}
 (a.output/'recovery-protocol.json').write_text(json.dumps(recovery_protocol,indent=2));start=time.monotonic();fatal=None;retried_tasks=1
 try:
  for trial,task in pending:
   print('RECOVER_START',trial,task.id,'preserved',len(results.simulations),flush=True)
   error_start=len(client.failures)
   def invoke():return run_single_task(config,task,seed=300+trial,evaluation_type=EvaluationType.ALL,save_dir=a.output/'official-logs',verbose_logs=True)
   sim=run_with_retry(invoke,task,trial,300+trial,max_retries=0,console_display=False)
   if sim.termination_reason==TerminationReason.INFRASTRUCTURE_ERROR:
    persist_error(sim,'recovery_provider_or_runtime_failure')
    if bridge.budget_exhausted:raise RuntimeError('Budget exhausted; stop without pretending completion')
    if len(client.failures)>error_start:
     retried_tasks+=1;sim=run_with_retry(invoke,task,trial,300+trial,max_retries=0,console_display=False)
     if sim.termination_reason==TerminationReason.INFRASTRUCTURE_ERROR:persist_error(sim,'bounded_second_attempt_failed')
   if bridge.budget_exhausted:raise RuntimeError('Budget exhausted')
   sim.trial=trial;sim.seed=300+trial;results.simulations.append(sim);results.save(a.output/'results.json')
   assert trajectory_digest(results.simulations[:count])==source_hash
   report={'finished':len(results.simulations),'expected':200,'passed_tasks':sum(s.reward_info is not None and s.reward_info.reward==1 for s in results.simulations),'infrastructure_error_attempts':len(failures),'task_recoveries':retried_tasks};(a.output/'progress.json').write_text(json.dumps(report,indent=2));print('RECOVER_DONE',trial,task.id,sim.reward_info.reward if sim.reward_info else None,sim.termination_reason,flush=True)
 except Exception as exc:fatal=type(exc).__name__
 finally:
  try:bridge.close()
  except Exception:fatal=fatal or 'CleanupFailed'
 oldrows=[json.loads(x) for x in (a.original_run/'attempts/calls.jsonl').read_text().splitlines() if x.strip()];allrows=oldrows+bridge.records;(a.output/'attempts').mkdir();(a.output/'attempts/calls.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in allrows))
 complete=fatal is None and len(results.simulations)==200 and all(s.reward_info is not None for s in results.simulations)
 summary={**protocol,'completed':complete,'full_coverage':complete,'finished':len(results.simulations),'expected':200,'passed_tasks':sum(s.reward_info is not None and s.reward_info.reward==1 for s in results.simulations),'original_prefix_unchanged':trajectory_digest(results.simulations[:count])==source_hash,'calls':len(allrows),'known_peak_cost_cny':sum(r.get('conservative_peak_cost_cny') or 0 for r in allrows),'unknown_cost_calls':sum(r.get('conservative_peak_cost_cny') is None for r in allrows),'session_spent_or_reserved_cny':bridge.budget.spent,'authorized_total_cny_limit':20,'primary_execution_seconds':json.loads((a.original_run/'summary.json').read_text())['seconds'],'recovery_execution_seconds':time.monotonic()-start,'failure_type':fatal,'infrastructure_error_attempts':len(failures),'task_recoveries':retried_tasks,'gpt_calls':0}
 (a.output/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary),flush=True)
 raise SystemExit(0 if complete else 1)
if __name__=='__main__':main()
