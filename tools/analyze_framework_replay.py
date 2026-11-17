"""Analyze complete matched replay blocks without new model-quality claims."""
import argparse,json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser();parser.add_argument('matrix',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();matrix=json.loads(args.matrix.read_text())
    assert matrix['completed'] and matrix['supplier_requests']==0 and len(matrix['rows'])==12
    rows=matrix['rows'];arms={};paired=[]
    for fixture in ('recorded-runtime','recorded-direct'):
        subset=[r for r in rows if r['fixture']==fixture]
        assert len(subset)==6 and len({r['request_sequence_sha256'] for r in subset})==1
        assert all(r['all_identical'] and r['cases']==50 and r['supplier_requests']==0 and r['rewards']==subset[0]['rewards'] for r in subset)
        for repeat in (0,1):
            group={r['arm']:r for r in subset if r['repeat']==repeat};assert set(group)=={'baseline','candidate','direct'}
            assert len({r['host_cohort'] for r in group.values()})==1,'Do not compare arms from different boots within a block'
            paired.append({'fixture':fixture,'repeat':repeat,'host_cohort':group['baseline']['host_cohort'],
                'candidate_seconds_ratio':group['candidate']['seconds']/group['baseline']['seconds'],
                'candidate_cpu_ratio':group['candidate']['cgroup_cpu_usec']/group['baseline']['cgroup_cpu_usec']})
    for arm in ('baseline','candidate','direct'):
        selected=[r for r in rows if r['arm']==arm]
        assert len(selected)==4
        arms[arm]={'case_executions':sum(r['cases'] for r in selected),
            'seconds':sum(r['seconds'] for r in selected),
            'cgroup_cpu_seconds':sum(r['cgroup_cpu_usec'] for r in selected)/1e6,
            'cgroup_memory_peak_bytes_max':max(r['cgroup_memory_peak_bytes'] for r in selected),
            'source_commits':sorted({r['source'] for r in selected})}
    report={'passed':True,'supplier_requests':0,'unique_recorded_conversations':100,'repetitions':2,
        'case_executions':600,'all_request_dialogue_reward_checks_identical':True,'arms':arms,'paired_blocks':paired,
        'candidate_time_reduction_vs_old_runtime':1-arms['candidate']['seconds']/arms['baseline']['seconds'],
        'candidate_cpu_reduction_vs_old_runtime':1-arms['candidate']['cgroup_cpu_seconds']/arms['baseline']['cgroup_cpu_seconds'],
        'candidate_time_ratio_vs_direct':arms['candidate']['seconds']/arms['direct']['seconds'],
        'external_restart':matrix.get('external_restart',False),'discarded_partial_runs':matrix.get('discarded_partial_runs',0),
        'original_live_scores_unchanged':{'runtime':39,'direct':41,'tasks_each':50},
        'official_pr':'https://github.com/sierra-research/tau2-bench/pull/763','official_pr_state':'draft','official_rank':None,
        'limits':['Recorded replies are fixtures, not fresh inference or a new capability score',
            'Request hashes, tool executions, full dialogue and ALL reward stay identical; supplier token/billing changes are not claimed',
            'Measured optimization source is 4cf27b8; later multi-API additions are not part of these timing samples',
            'Shared page/page-cache ownership can affect cgroup memory attribution; peak differences do not prove intrinsic memory reduction',
            'An external host reboot interrupted one partial sample; all three arms of the final matched block were rerun after reboot; no partial samples included',
            'Runtime remains slower and consumes more local resources than bare direct execution']}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
