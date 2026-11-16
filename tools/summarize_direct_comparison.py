"""Recompute paired benchmark tables; missing provider usage stays missing."""
import argparse
import csv
import json
from pathlib import Path
import statistics


def aggregate(rows):
    calls=[c for r in rows for c in r['calls']]
    usage_complete=all(isinstance(c.get('usage'),dict) and all(type(c['usage'].get(k)) is int for k in ('prompt_tokens','completion_tokens')) for c in calls)
    value={'samples':len(rows),'passed':sum(r['evaluation']['task_passed'] for r in rows),
        'json_format_ok':sum(r['evaluation']['json_format_ok'] for r in rows),'call_intents':len(calls),
        'returned_calls':sum(c['status']=='returned' for c in calls),'usage_complete':usage_complete,
        'median_wall_seconds':statistics.median(r['wall_seconds'] for r in rows),
        'wall_range_seconds':[min(r['wall_seconds'] for r in rows),max(r['wall_seconds'] for r in rows)],
        'median_local_cpu_seconds':statistics.median(r['local_cpu_seconds'] for r in rows),
        'median_cgroup_memory_peak_bytes':statistics.median(r['cgroup_memory_peak_bytes'] for r in rows),
        'delivered_patch_verified_same_hash':sum(r['evaluation'].get('runtime_verified_same_patch',False) for r in rows),
        'any_successful_protected_patch_test':sum(any(t.get('plugin')=='project.patch_check' and t.get('success') for t in r.get('tool_summary',[])) for r in rows)}
    for key in ('prompt_tokens','completion_tokens'):
        value[key]=sum(c['usage'][key] for c in calls) if usage_complete else None
    hits=[c.get('usage',{}).get('prompt_cache_hit_tokens',c.get('usage',{}).get('prompt_tokens_details',{}).get('cached_tokens')) for c in calls]
    value['cached_input_tokens']=sum(hits) if hits and all(type(x) is int for x in hits) else None
    value['uncached_input_tokens']=value['prompt_tokens']-value['cached_input_tokens'] if value['prompt_tokens'] is not None and value['cached_input_tokens'] is not None else None
    return value


def summarize(primary,supplement,output):
    assert primary['finished'] and len(primary['rows'])==42
    assert supplement['finished'] and len(supplement['rows'])==12
    ids={(r['case'],r['arm']) for r in primary['rows']};assert len(ids)==42
    assert all(c['status']=='returned' for r in primary['rows']+supplement['rows'] for c in r['calls'])
    groups={}
    for source in (primary,supplement):
        for group in sorted(set(r['group'] for r in source['rows'])):
            groups[group]={arm:aggregate([r for r in source['rows'] if r['group']==group and r['arm']==arm]) for arm in sorted(set(r['arm'] for r in source['rows'] if r['group']==group))}
    summary={'primary_pairs':21,'primary_samples':42,'supplement_samples':12,
        'total_request_intents':primary['paid_call_intents']+supplement['extra_request_intents'],
        'groups':groups,'runtime_commit':primary['protocol']['runtime_commit'],
        'provider_models':sorted({c.get('model') for r in primary['rows']+supplement['rows'] for c in r['calls']}),
        'currency_cost':None,'global_no_paid_post_replay':True,
        'cleanup_counter_note':'Original remaining_sample_cgroups counts virtual control files as well as directories. Retained unchanged; use cleanup-verification.json and the completed per-sample populated/rmdir checks.',
        'raw_cleanup_counters':[primary['remaining_sample_cgroups'],supplement['remaining_sample_cgroups']]}
    assert summary['total_request_intents']<=96
    direct=groups['knowledge']['direct'];runtime=groups['knowledge']['runtime'];matched=groups['knowledge_context_matched']['direct_context']
    summary['knowledge_input_reduction_vs_full_context']=1-runtime['prompt_tokens']/direct['prompt_tokens']
    summary['knowledge_runtime_input_ratio_vs_matched']=runtime['prompt_tokens']/matched['prompt_tokens']
    Path(output).write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    flat=Path(output).with_suffix('.csv')
    with flat.open('w',newline='',encoding='utf-8-sig') as file:
        fields=['study','group','case','arm','passed','wall_seconds','local_cpu_seconds','cgroup_memory_peak_bytes','calls','input_tokens','output_tokens']
        writer=csv.DictWriter(file,fieldnames=fields);writer.writeheader()
        for study,data in [('primary',primary),('supplement',supplement)]:
            for r in data['rows']:
                writer.writerow({'study':study,'group':r['group'],'case':r['case'],'arm':r['arm'],'passed':r['evaluation']['task_passed'],
                    'wall_seconds':r['wall_seconds'],'local_cpu_seconds':r['local_cpu_seconds'],'cgroup_memory_peak_bytes':r['cgroup_memory_peak_bytes'],
                    'calls':len(r['calls']),'input_tokens':sum(c['usage']['prompt_tokens'] for c in r['calls']),'output_tokens':sum(c['usage']['completion_tokens'] for c in r['calls'])})
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('primary');p.add_argument('supplement');p.add_argument('output');a=p.parse_args()
    summarize(json.loads(Path(a.primary).read_text(encoding='utf-8')),json.loads(Path(a.supplement).read_text(encoding='utf-8')),a.output)
