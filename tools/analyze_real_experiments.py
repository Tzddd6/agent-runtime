"""Aggregate source-backed live/replay experiments without mixing their costs.

Input is a JSON array. Output is a strict allowlist: no prompts, model responses,
keys, command output or arbitrary extra metadata are copied into the reports.
Rates, when supplied, produce estimates only and never a vendor billing claim.
"""
import argparse
import csv
import json
import math
from pathlib import Path
import re
import statistics


KINDS = ('live', 'replay', 'synthetic')
USAGE_FIELDS = ('prompt_tokens', 'completion_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')
GROUP_FIELDS = ('kind', 'scenario', 'workers', 'policy', 'cache_mode', 'model')
METRICS = ('wall_seconds', 'ready_wait_seconds', 'max_ready_wait_seconds', 'cpu_seconds', 'peak_memory_bytes')
IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,128}')


def identifier(value, field):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(field + ' must be a short identifier')
    if re.search(r'(?i)sk-[a-z0-9]{16,}',value):
        raise ValueError(field + ' resembles credential material; refusing report')
    return value


def number(value, field, integer=False, signed=False):
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (not signed and value < 0)
            or (integer and type(value) is not int)):
        raise ValueError(field + ' must be a finite ' + ('integer' if integer else 'number'))
    return value


def test_counts(value, depth=0):
    """Keep numerical verification evidence, never arbitrary stdout or source."""
    if value is None:
        return None
    if not isinstance(value, dict) or depth > 2:
        raise ValueError('test_results must contain bounded count dictionaries')
    result = {}
    for key in ('public', 'heldout', 'baseline', 'patched', 'before', 'after'):
        if key in value:
            result[key] = test_counts(value[key], depth+1)
    for key in ('passed', 'failed', 'errors', 'skipped', 'tests_run', 'total', 'returncode'):
        if key in value:
            result[key] = number(value[key], 'test_results.'+key, integer=True, signed=key=='returncode')
    for key in ('success', 'accepted'):
        if key in value:
            if type(value[key]) is not bool:
                raise ValueError('test result flags must be booleans')
            result[key] = value[key]
    return result


def normalize(record):
    if not isinstance(record, dict):
        raise ValueError('each record must be an object')
    if record.get('kind') not in KINDS:
        raise ValueError('kind must be live, replay or synthetic')
    if type(record.get('success')) is not bool:
        raise ValueError('success must be an observed boolean')
    workers = number(record.get('workers'), 'workers', integer=True)
    if workers is None or workers < 1:
        raise ValueError('workers must be a positive integer')
    result = {field: identifier(record.get(field, default), field) for field, default in (
        ('id', None), ('scenario', 'unspecified'), ('policy', None),
        ('cache_mode', 'unspecified'), ('model', 'unspecified'))}
    result.update(kind=record['kind'], workers=workers, success=record['success'])
    for field in ('wall_seconds', 'cpu_seconds', 'peak_memory_bytes'):
        result[field] = number(record.get(field), field, integer=field=='peak_memory_bytes')
    if result['wall_seconds'] is None:
        raise ValueError('wall_seconds is required')
    waits = record.get('ready_wait_seconds')
    if isinstance(waits, dict):
        clean_waits = {identifier(key, 'task_id'):number(value, 'ready_wait_seconds')
                       for key,value in waits.items()}
        observed = [value for value in clean_waits.values() if value is not None]
        result['task_ready_wait_seconds'] = clean_waits
        result['ready_wait_seconds'] = statistics.mean(observed) if observed else None
        result['max_ready_wait_seconds'] = max(observed) if observed else None
    else:
        result['ready_wait_seconds'] = number(waits, 'ready_wait_seconds')
        result['max_ready_wait_seconds'] = number(record.get('max_ready_wait_seconds'), 'max_ready_wait_seconds')
    usage = record.get('usage') or {}
    if not isinstance(usage, dict):
        raise ValueError('usage must be an object')
    result['usage'] = {field:number(usage.get(field), 'usage.'+field, integer=True) for field in USAGE_FIELDS}
    result['model_calls'] = number(record.get('model_calls'), 'model_calls', integer=True)
    result['billed_model_calls'] = number(record.get('billed_model_calls'), 'billed_model_calls', integer=True)
    if result['kind'] != 'live':
        if result['billed_model_calls'] not in (None,0):
            raise ValueError('replay and synthetic cannot report billed model calls')
        result['billed_model_calls'] = 0
    if (result['model_calls'] is not None and result['billed_model_calls'] is not None
            and result['billed_model_calls'] > result['model_calls']):
        raise ValueError('billed model calls cannot exceed recorded calls')
    result['usage_source'] = identifier(record.get('usage_source', 'unknown'), 'usage_source')
    result['test_results'] = test_counts(record.get('test_results'))
    source_hash = record.get('source_sha256')
    if source_hash is not None:
        if not isinstance(source_hash,str) or not re.fullmatch('[0-9a-f]{64}',source_hash):
            raise ValueError('source_sha256 must be SHA256 hex')
        result['source_sha256'] = source_hash
    return result


def normalize_rates(rates):
    if rates is None:
        return None
    if not isinstance(rates,dict):
        raise ValueError('rates must be an object')
    result = {'currency':identifier(rates.get('currency'), 'currency')}
    for field in ('input_per_million','output_per_million','cached_input_per_million'):
        result[field] = number(rates.get(field), field)
    if result['input_per_million'] is None or result['output_per_million'] is None:
        raise ValueError('input/output rates are required')
    if rates.get('model') is not None:
        result['model'] = identifier(rates['model'],'model')
    return result


def estimate(record, rates):
    if record['kind'] != 'live':
        return 0.0
    if rates is None or (rates.get('model') and record['model'] != rates['model']):
        return None
    usage = record['usage']
    prompt, completion = usage['prompt_tokens'], usage['completion_tokens']
    if prompt is None or completion is None:
        return None
    hit = usage['prompt_cache_hit_tokens']
    miss = usage['prompt_cache_miss_tokens']
    if hit is not None and rates['cached_input_per_million'] is not None:
        if hit > prompt or (miss is not None and miss+hit != prompt):
            raise ValueError('provider cache token counts disagree with prompt_tokens')
        input_cost = hit*rates['cached_input_per_million']+(prompt-hit)*rates['input_per_million']
    else:
        # Without cache evidence this deliberately does not invent a discount.
        input_cost = prompt*rates['input_per_million']
    return (input_cost+completion*rates['output_per_million'])/1_000_000


def observed_summary(values):
    values = [value for value in values if value is not None]
    return {'observations':len(values), 'median':statistics.median(values) if values else None,
            'min':min(values) if values else None, 'max':max(values) if values else None}


def summarize(records, rates=None):
    if not isinstance(records,list) or not records:
        raise ValueError('input must be a nonempty record array')
    clean = [normalize(record) for record in records]
    if len({row['id'] for row in clean}) != len(clean):
        raise ValueError('record IDs must be unique')
    rates = normalize_rates(rates)
    buckets = {}
    for row in clean:
        row['estimated_api_cost'] = estimate(row,rates)
        row['actual_api_cost'] = None
        buckets.setdefault(tuple(row[field] for field in GROUP_FIELDS),[]).append(row)
    groups = []
    for key, rows in buckets.items():
        group = dict(zip(GROUP_FIELDS,key))
        group.update(n=len(rows), successes=sum(row['success'] for row in rows),
                     completion_rate=sum(row['success'] for row in rows)/len(rows),
                     small_sample_warning=len(rows)<3)
        group['metrics'] = {field:observed_summary([row[field] for row in rows]) for field in METRICS}
        group['usage'] = {field:{'observations':sum(row['usage'][field] is not None for row in rows),
                               'observed_total':sum(row['usage'][field] for row in rows if row['usage'][field] is not None)}
                          for field in USAGE_FIELDS}
        costs = [row['estimated_api_cost'] for row in rows]
        group['estimated_api_cost'] = sum(costs) if all(cost is not None for cost in costs) else None
        group['actual_api_cost'] = None
        group['record_ids'] = [row['id'] for row in rows]
        groups.append(group)
    totals = {}
    for kind in KINDS:
        rows = [row for row in clean if row['kind']==kind]
        if not rows:
            continue
        costs = [row['estimated_api_cost'] for row in rows]
        totals[kind] = {
            'records':len(rows),'successes':sum(row['success'] for row in rows),
            'failed_records':sum(not row['success'] for row in rows),
            'estimated_api_cost':sum(costs) if all(cost is not None for cost in costs) else None,
            'actual_api_cost':None,
            'usage':{field:{'observations':sum(row['usage'][field] is not None for row in rows),
                            'observed_total':sum(row['usage'][field] for row in rows if row['usage'][field] is not None)}
                     for field in USAGE_FIELDS}}
    return {'methodology':{
        'separate_kinds':True, 'missing_values':'null means not measured; never substituted with zero',
        'replay_usage':'logical source tokens may be retained; replay incurs zero new provider calls',
        'cost':'estimate only from supplied rates; actual vendor bill remains unknown',
        'failed_calls':'failed tasks, empty responses and diagnostic runs still count whenever provider usage exists; completion_tokens already includes reasoning tokens',
        'small_samples':'n=1 is a single observation, not evidence of statistical significance',
        'budget_confound':'if worker count also changes admission CPU budget, serial/parallel results are their joint effect, not a pure parallelism intervention',
        'provider_confound':'remote latency, vendor prefix cache warming, request order and nondeterministic model outputs may affect time/cost independently of the local scheduler and JSON cache',
        'scope':'one controlled project fixture cannot establish benefits for arbitrary real repositories',
        'wait_metric':'per-run mean/max of dependency-ready task wait values; excludes unmet-dependency time',
        'peak_memory':'must be measured simultaneously for the workflow, not summed task peaks',
        'redaction':'allowlisted metadata/counts only; prompts, answers, keys, stdout and extra fields excluded'},
        'rates':rates,'totals_by_kind':totals,'groups':groups,'records':clean}


def display(value):
    return '—' if value is None else f'{value:.6g}' if isinstance(value,float) else str(value)


def write_report(report, directory):
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    (directory/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    columns = ['id',*GROUP_FIELDS,'success',*METRICS,'model_calls','billed_model_calls',
               *USAGE_FIELDS,'estimated_api_cost','actual_api_cost','usage_source','source_sha256','test_results']
    with (directory/'records.csv').open('w',encoding='utf-8-sig',newline='') as target:
        writer=csv.DictWriter(target,fieldnames=columns)
        writer.writeheader()
        for row in report['records']:
            flattened={key:row.get(key) for key in columns}
            flattened.update(row['usage'])
            flattened['test_results']=json.dumps(row['test_results'],ensure_ascii=False)
            writer.writerow(flattened)
    lines=['# Real workload experiment results','',
           'Live provider calls, fixed-result replay and synthetic fixtures are separate groups. '
           'Missing measurements are shown as —, not zero. Token-derived costs are estimates; actual vendor billing is unknown.',
           '', '| Kind | Scenario | Workers | Policy | Cache | n | Completed | Wall median (s) | Ready wait mean median (s) | CPU median (s) | Workflow peak RAM median (bytes) | Estimated API cost |',
           '|---|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for group in report['groups']:
        metrics=group['metrics']
        values=[group['kind'],group['scenario'],group['workers'],group['policy'],group['cache_mode'],
                group['n'],f"{group['successes']}/{group['n']}",metrics['wall_seconds']['median'],
                metrics['ready_wait_seconds']['median'],metrics['cpu_seconds']['median'],
                metrics['peak_memory_bytes']['median'],group['estimated_api_cost']]
        lines.append('| '+' | '.join(display(value) for value in values)+' |')
    if any(group['small_sample_warning'] for group in report['groups']):
        lines.extend(['','**Small-sample limitation:** groups with fewer than three observations, especially n=1, '
                      'cannot establish statistically reliable speedup. Provider/network latency and nondeterministic answers remain confounders.'])
    lines.extend(['','Replay token counts describe the original model outputs; only new live requests consume additional provider quota. '
                  'CPU/memory values require OS measurements; sums of individual task memory peaks are not workflow peak memory.',
                  '', '**Cost completeness:** estimates include failed attempts, empty-content responses and diagnostic runs when provider token usage is present. '
                  'A failed functional result does not imply a free API call. Completion tokens already include reasoning tokens; do not add them twice.',
                  '', '**Comparison limitations:** worker count may also change the controller admission CPU budget; in that case serial/parallel is a joint intervention, '
                  'not isolated parallel speedup. Remote latency, vendor prefix-cache warming, request order and nondeterministic answers remain separate confounders. '
                  'Vendor cache is different from the application JSON LRU-2 cache. One controlled project fixture cannot establish results for arbitrary repositories.',
                  '', 'Rate currency: '+(report['rates']['currency'] if report['rates'] else 'not supplied; live cost estimates unavailable')+'.'])
    for kind,total in report['totals_by_kind'].items():
        lines.append(f"{kind}: {total['records']} records, {total['failed_records']} failed; "
                     f"estimated API cost including failures = {display(total['estimated_api_cost'])}; actual bill unknown.")
    (directory/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('records',type=Path)
    parser.add_argument('--rates',type=Path,help='optional JSON price rates per million tokens')
    parser.add_argument('--output-dir',type=Path,default=Path('reports/real-experiments'))
    args=parser.parse_args()
    try:
        records=json.loads(args.records.read_text(encoding='utf-8-sig'))
        rates=json.loads(args.rates.read_text(encoding='utf-8-sig')) if args.rates else None
        report=summarize(records,rates)
        write_report(report,args.output_dir)
    except (OSError,ValueError,TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps({'records':len(report['records']),'groups':len(report['groups']),
                      'output_dir':str(args.output_dir)},ensure_ascii=False))


if __name__=='__main__':main()
