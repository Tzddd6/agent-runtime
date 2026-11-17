"""Recompute this interrupted 12-task paired evaluation without hiding failure."""
import argparse,json,statistics
from pathlib import Path

def analyze(first_rows,first_ledger,continuation,continuation_ledger):
 if len(first_rows)!=6 or len(first_ledger['calls'])!=9 or len(continuation['rows'])!=5:raise ValueError('Unexpected interrupted run structure')
 failed=first_ledger['calls'][-1]
 if failed['state']!='failed_billing_unknown':raise ValueError('Missing original failed request')
 failure={'repeat':1,'case':'structured','arm':'runtime','seconds':failed['seconds'],'seconds_is_lower_bound':True,'timing_scope':'provider-broker call only; original outer timer unavailable after driver aborted','passed':False,'request_hashes':[failed['request_sha256']],'calls':1,'usage':{'prompt_tokens':0,'completion_tokens':0},'usage_unknown_calls':1,'coordinator_cpu_seconds':None,'broker_seconds':None,'provider_seconds':None,'case_error_type':'ModelError','cohort':'original_recorded_failure_not_replayed'}
 rows=first_rows+[failure]+continuation['rows']
 for row in rows:row.setdefault('usage_unknown_calls',0)
 keys=[(r['repeat'],r['case'],r['arm']) for r in rows]
 if len(set(keys))!=12 or set(keys)!={(i,c,a) for i in (0,1) for c in ('structured','repair','tool_chain') for a in ('direct','runtime')}:raise ValueError('Task identity mismatch')
 for i in (0,1):
  for case in ('structured','repair','tool_chain'):
   pair=[next(r for r in rows if r['repeat']==i and r['case']==case and r['arm']==arm) for arm in ('direct','runtime')]
   if pair[0]['request_hashes']!=pair[1]['request_hashes']:raise ValueError('Provider request mismatch')
 if sum(r['calls'] for r in rows)!=16 or len(first_ledger['calls'])+len(continuation_ledger['calls'])!=16:raise ValueError('Paid attempt accounting mismatch')
 summary={'source_measured':'4c455354f776cc4babe837710cb7ee2198d485d0','provider':'aizex','model_identifier':'gpt-6-luna','model_identity_independently_verified':False,'tasks_each':6,'paid_attempts':16,'paired_provider_requests_identical':True,'original_failed_case_replayed':False,'arms':{},'rows':rows,'limits':['Interrupted driver was resumed only at unattempted cases; failed case included, never replayed','Runtime failed-task duration is broker-call lower bound; original outer timer/CPU were not captured','Failed call usage and actual billing remain unknown; known usage is not a full token total','Single small synthetic suite repeated twice, not an official capability score or statistical significance result','Direct shares the same broker, provider adapter and network; SDK run path, not full web automatic routing','Runtime and direct get the same explicit tool returns, canonicalized messages and output limits','Relay model identifier and usage are supplier-reported; cannot verify underlying model identity','No evidence that Runtime is faster or uses fewer total tokens in this run']}
 for arm in ('direct','runtime'):
  selected=[r for r in rows if r['arm']==arm];known=[r for r in selected if not r['usage_unknown_calls']]
  summary['arms'][arm]={'passed':sum(r['passed'] for r in selected),'tasks':len(selected),'model_attempts':sum(r['calls'] for r in selected),'task_seconds_sum_or_lower_bound':sum(r['seconds'] for r in selected),'task_seconds_is_lower_bound':any(r.get('seconds_is_lower_bound') for r in selected),'usage_known':{k:sum(r['usage'][k] for r in selected) for k in ('prompt_tokens','completion_tokens')},'usage_unknown_calls':sum(r['usage_unknown_calls'] for r in selected),'known_provider_seconds':sum(r['provider_seconds'] for r in known),'local_over_broker_seconds_per_known_call':sum(r['seconds']-r['broker_seconds'] for r in known)/sum(r['calls'] for r in known),'coordinator_cpu_seconds_known':sum(r['coordinator_cpu_seconds'] or 0 for r in selected),'coordinator_cpu_unknown_tasks':sum(r['coordinator_cpu_seconds'] is None for r in selected)}
 ledger=first_ledger['calls']+continuation_ledger['calls']
 summary['cost']={'currency':'nominal USD at user-supplied relay token prices','known_usage_estimate':sum((r['usage']['prompt_tokens']*.1+r['usage']['completion_tokens']*.5)/1e6 for r in ledger if 'usage' in r),'conservative_reserved':first_ledger['reserved']+continuation_ledger['reserved'],'unknown_billing_calls':sum('usage' not in r for r in ledger),'actual_provider_bill_checked':False,'discounts_ignored':True}
 summary['transport']={}
 for phase,records in [('original',first_ledger['calls']),('continuation',continuation_ledger['calls'])]:
  known=[r for r in records if 'transport_timings' in r]
  summary['transport'][phase]={'completed_calls':len(known),'total_status_reads':sum(r['transport_timings']['status_reads'] for r in known),'median_detection_seconds':statistics.median(r['transport_timings']['completion_detection_seconds'] for r in known)}
 return summary

def main():
 p=argparse.ArgumentParser();p.add_argument('--first-rows',type=Path,required=True);p.add_argument('--first-ledger',type=Path,required=True);p.add_argument('--continuation',type=Path,required=True);p.add_argument('--continuation-ledger',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 data=analyze(*(json.loads(path.read_text()) for path in (a.first_rows,a.first_ledger,a.continuation,a.continuation_ledger)));a.output.write_text(json.dumps(data,indent=2)+'\n');print(json.dumps({k:v for k,v in data.items() if k!='rows'}))
if __name__=='__main__':main()
