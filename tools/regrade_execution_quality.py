"""Recheck unchanged model outputs with an absolute-path external oracle.

The frozen main harness used a relative checker path for controlled projects.
Original raw grades stay intact. This command makes no supplier requests.
"""
import hashlib,json,statistics,sys
from pathlib import Path
REPO=Path(__file__).resolve().parents[1];sys.path.insert(0,str(REPO))
from tools.benchmark_engineering_loop import grade
root=Path(sys.argv[1]).resolve();document=json.loads((root/'results.json').read_text())
cases=json.loads((root/'dataset.json').read_text())['cases'];lookup={case['id']:(index,case) for index,case in enumerate(cases)}
oracle=root/'absolute-oracle';oracle.mkdir(exist_ok=False)
rows=[]
for row in document['rows']:
    index,case=lookup[row['case']];tag=f"{index:02d}-{row['arm']}"
    result=json.loads((root/(tag+'-result.json')).read_text())
    checked=grade(case,result,oracle,tag)
    rows.append({**row,'raw_quality':row['quality'],'quality':checked})
summary={}
for arm in ('direct_full','direct_compact','runtime_spawn','runtime_fork'):
    selected=[row for row in rows if row['arm']==arm]
    calls=[call for row in selected for call in row['calls']]
    summary[arm]={'cases':len(selected),'passed':sum(row['quality']['passed'] for row in selected),'calls':len(calls),
                  'input_tokens':sum((call.get('usage') or {}).get('prompt_tokens',0) for call in calls),
                  'output_tokens':sum((call.get('usage') or {}).get('completion_tokens',0) for call in calls),
                  'cached_input_tokens':sum((call.get('usage') or {}).get('prompt_cache_hit_tokens',0) for call in calls),
                  'median_seconds':statistics.median(row['seconds'] for row in selected),
                  'median_cpu_seconds':statistics.median(row['cpu_seconds'] for row in selected),
                  'median_memory_peak_bytes':statistics.median(row['memory_peak_bytes'] for row in selected),
                  'median_non_model_seconds':statistics.median(max(0,row['seconds']-sum(call.get('seconds',0) for call in row['calls'])) for row in selected)}
report={'raw_results_sha256':hashlib.sha256((root/'results.json').read_bytes()).hexdigest(),
        'new_model_calls':0,'original_results_unchanged':True,'correction':'absolute checker path only; same samples, prompts, verification, oracle and expected values',
        'summary':summary,'rows':rows,'paid_intents':document['paid_intents'],'remaining_sample_cgroups':document.get('remaining_sample_cgroups'),
        'limits':['small declared workload, not statistical significance','model sampling/cache/network not controlled','do not claim lower tokens without also reporting quality and all failed samples']}
(root/'corrected-quality.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({k:report[k] for k in ('summary','paid_intents','remaining_sample_cgroups')},ensure_ascii=False))
