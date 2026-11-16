"""Prepare a complete single-domain custom submission without private contact.

Only official trajectories and reviewed summaries are selected, never Attempt
payloads or private control configuration. CNY cost metadata is normalized to
official peak USD rates; tasks/messages/tools/usage/rewards remain identical.
"""
import argparse
import hashlib
import json
import shutil
from datetime import date
from pathlib import Path


def immutable_view(results):
    value=results.model_dump(mode='json')
    for sim in value['simulations']:
        sim.pop('agent_cost',None);sim.pop('user_cost',None)
        for message in sim.get('messages') or []:message.pop('cost',None)
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--implementation-sha',required=True)
    args=parser.parse_args()
    from tau2.data_model.simulation import Results as Trajectories
    from tau2.scripts.leaderboard.submission import Submission,Results,DomainResults,ContactInfo,Methodology,Verification,Reference
    from tau2.scripts.leaderboard.verify_trajectories_public import check_tasks,check_num_trials,check_format
    from tau2.scripts.leaderboard.prepare_submission import check_and_load_submission_data,validate_submission_traj_set,get_metrics
    from tau2.metrics.agent_metrics import compute_metrics
    summary=json.loads((args.run_dir/'summary.json').read_text())
    assert summary['completed'],'Incomplete runs cannot be submitted'
    original=Trajectories.load(args.run_dir/'runtime-results.json')
    assert original.info.git_commit==args.implementation_sha,'Unexpected source version'
    assert check_tasks(original)[0] and check_num_trials(original)[0]
    before=immutable_view(original)
    public=original.model_copy(deep=True)
    # Existing call budget records peak CNY. All three official USD list rates
    # are exactly 0.15 times the corresponding CNY list rates on 2026-11-17.
    # This is price normalization, not an exchange-rate claim or actual invoice.
    for sim in public.simulations:
        for key in ('agent_cost','user_cost'):
            value=getattr(sim,key)
            if value is not None:setattr(sim,key,value*.15)
        for message in sim.messages or []:
            if hasattr(message,'cost') and message.cost is not None:message.cost*=.15
    assert immutable_view(public)==before,'A non-cost trajectory field changed'
    args.output.mkdir(parents=True,exist_ok=False,mode=0o700)
    folder=args.output/'trajectories';folder.mkdir()
    name='agentruntime_deepseek_flash_airline_v101_1trial.json'
    public.save(folder/name)
    assert check_format(folder/name)[1]
    _,domain_metrics,model,user=get_metrics([public])
    data=Submission(model_name=model,model_organization='DeepSeek',submitting_organization='Agent Runtime (Tzddd6)',
        submission_date=date(2026,11,17),submission_type='custom',modality='text',
        contact_info=ContactInfo(github='Tzddd6'),results=Results(**domain_metrics),
        trajectories_available=True,trajectory_files={'airline':name},
        references=[Reference(title='Frozen Agent Runtime implementation',type='github',url='https://github.com/Tzddd6/agent-runtime/tree/'+args.implementation_sha)],
        methodology=Methodology(evaluation_date=date(2026,11,17),tau2_bench_version='1.0.1',user_simulator=user,
            notes='Custom native broker transport and per-turn Runtime Attempts. DeepSeek Flash (non-thinking, temperature 0, max_tokens 2048) is BOTH agent and user simulator; this is not the default GPT-4.1 simulator configuration. Official prompts, tasks, policies, tools and ALL evaluator unchanged. Airline base: every one of 50 tasks, one trial; no pass^2/3/4 claim. Per-task alternating order versus direct arm; all outcomes preserved. Cost metadata normalized to official Flash PEAK USD list rates dated 2026-11-17, not actual off-peak invoice. Private email withheld; please contact GitHub @Tzddd6.',
            verification=Verification(modified_prompts=False,omitted_questions=False,
                details='Official source fc0055dc4e0a316c3f83133267fbd6faaa770992, byte hashes verified before every task. Runtime implementation '+args.implementation_sha+'. Official tools execute in the coordinator under a service cgroup; only model inference uses Runtime Attempts. No hidden expected actions supplied to agent.')))
    (args.output/'submission.json').write_text(data.model_dump_json(indent=2,exclude_none=True))
    ok,error,loaded=check_and_load_submission_data(str(args.output))
    assert ok,error
    assert validate_submission_traj_set(loaded.results)[0]
    assert check_tasks(loaded.results[0])[0] and check_num_trials(loaded.results[0])[0]
    _,recomputed,_,_=get_metrics(loaded.results)
    assert data.results.airline.model_dump()==recomputed['airline'].model_dump()
    audit={'official_format':True,'all_tasks_identical':True,'trial_counts_valid':True,
           'metrics_recomputed':True,'non_cost_trajectory_sha256':before,
           'private_email_included':False,'supplier_key_included':False,
           'contact_policy':'GitHub contact only; maintainer acceptance not assumed',
           'ranking':None,'review_status':'not_submitted'}
    (args.output/'local-validation.json').write_text(json.dumps(audit,indent=2))
    print(json.dumps({'pass_1':data.results.airline.pass_1,'tasks':len(public.tasks),'audit':audit}))


if __name__=='__main__':main()
