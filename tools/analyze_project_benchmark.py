"""Curate local repair evidence without exporting prompts, credentials or source."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import statistics
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'reports/project-benchmark-20261111/'
DEPS = {'baseline': [], 'analyze': ['baseline'], 'test_plan': ['baseline'],
        'repair': ['analyze', 'test_plan'], 'validate': ['repair'],
        'review': ['validate'], 'gate': ['validate', 'review']}
PRICES = {'input_cache_miss': 2, 'input_cache_hit': .04, 'output': 8}


def stats(values):
    values = [x for x in values if isinstance(x, (int, float))]
    return {'n': len(values), 'median': statistics.median(values) if values else None,
            'min': min(values) if values else None, 'max': max(values) if values else None}


def estimate(usage):
    prompt = usage.get('prompt_tokens', 0)
    output = usage.get('completion_tokens', 0)
    hit = min(prompt, usage.get('prompt_cache_hit_tokens', 0))
    # Unknown input cache status is charged as a miss for this estimate.
    miss = prompt - hit
    return {'estimated_cny': (miss * 2 + hit * .04 + output * 8) / 1e6,
            'all_input_miss_upper_cny': (prompt * 2 + output * 8) / 1e6}


def event_cpu(event):
    raw = event.get('usage', {}).get('cpu.stat', '')
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == 'usage_usec':
            return int(fields[1]) / 1e6
    return None


def measurements(detail):
    starts, ends, cpus = {}, {}, []
    for event in detail.get('events', []):
        task, state = event['task_id'], event['state']
        if state == 'running':
            starts.setdefault(task, event['time'])
        elif state in ('succeeded', 'failed', 'timeout', 'cancelled'):
            ends[task] = event['time']
            cpu = event_cpu(event)
            if cpu is not None:
                cpus.append(cpu)
    waits = {}
    for task, start in starts.items():
        deps = DEPS.get(task, [])
        if all(dep in ends for dep in deps):
            ready = max(ends[dep] for dep in deps) if deps else detail['created']
            waits[task] = max(0, start - ready)
    sampled = [h['memory_bytes'] for h in detail.get('history', [])
               if isinstance(h.get('memory_bytes'), (int, float))]
    return {'ready_wait_seconds_by_task': waits,
            'ready_wait_sum_seconds': sum(waits.values()),
            'worker_cpu_seconds': sum(cpus) if cpus else None,
            'worker_cpu_terminal_records': len(cpus),
            'max_sampled_worker_memory_bytes': max(sampled) if sampled else None}


def failure_class(row):
    if row.get('success') is True:
        return None
    validation = row.get('validation') or {}
    error = validation.get('error')
    if error:
        lowered = error.lower()
        if any(word in lowered for word in ('subset', 'ast', 'attribute', 'signature', 'allowed')):
            return 'patch_policy_rejected'
        if 'json' in lowered:
            return 'patch_json_invalid'
        return 'patch_validation_error'
    if validation.get('success') is False:
        return 'immutable_tests_failed'
    if (row.get('verdict') or {}).get('accepted') is False:
        return 'review_or_hash_rejected'
    return 'execution_or_unknown_failure'


def summarize(rows):
    usage = Counter()
    for row in rows:
        usage.update(row['usage'])
    return {'n': len(rows), 'success_count': sum(r['success'] for r in rows),
            'failure_count': sum(not r['success'] for r in rows),
            'wall_seconds_all': stats([r['wall_seconds'] for r in rows]),
            'wall_seconds_success_only': stats([r['wall_seconds'] for r in rows if r['success']]),
            'ready_wait_sum_seconds': stats([r['ready_wait_sum_seconds'] for r in rows]),
            'worker_cpu_seconds': stats([r['worker_cpu_seconds'] for r in rows]),
            'max_sampled_worker_memory_bytes': stats([r['max_sampled_worker_memory_bytes'] for r in rows]),
            'usage': dict(usage), 'model_calls': sum(r['model_calls'] for r in rows),
            'usage_complete_count': sum(r['usage_complete'] for r in rows),
            'failure_classes': dict(Counter(r['failure_class'] for r in rows if not r['success'])),
            **estimate(usage)}


def generate(archive, output, document):
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as evidence:
        names = set(evidence.namelist())
        manifest = json.loads(evidence.read(PREFIX + 'manifest.json'))
        rows = []
        for plan in manifest['schedule']:
            stem = PREFIX + 'runs/' + str(plan['index']).zfill(3) + '/'
            if stem + 'metrics.json' not in names:
                continue
            metric = json.loads(evidence.read(stem + 'metrics.json'))
            detail = json.loads(evidence.read(stem + 'detail.json'))
            # Strict whitelist: never curate raw prompts/model output/patch/error text.
            row = {key: metric[key] for key in ('index', 'repeat', 'project', 'workers',
                   'policy', 'success', 'wall_seconds', 'model_calls', 'usage_complete')}
            row['usage'] = {k: v for k, v in metric.get('usage', {}).items()
                            if type(v) is int and v >= 0}
            row['failure_class'] = failure_class(metric)
            row['source_paths'] = [stem + 'metrics.json', stem + 'detail.json']
            row.update(measurements(detail))
            row.update(estimate(row['usage']))
            rows.append(row)
        def extra(path):
            if path not in names:
                return {'available': False, 'model_calls': 0, 'usage': {}, **estimate({})}
            detail = json.loads(evidence.read(path))
            usage, calls = Counter(), 0
            for event in detail['events']:
                call = (event.get('result') or {}).get('model_call')
                if call:
                    calls += 1
                    usage.update({k: v for k, v in call.get('usage', {}).items()
                                  if type(v) is int and v >= 0})
            return {'available': True, 'source_path': path, 'model_calls': calls,
                    'usage': dict(usage), **estimate(usage)}
        handoff = extra('reports/handoff/detail.json')
        gui_smoke = extra('reports/gui-smoke/detail.json')
        handoff_usage, handoff_calls = handoff['usage'], handoff['model_calls']
    grouped, planned = defaultdict(list), Counter()
    for plan in manifest['schedule']:
        planned[(plan['project'], plan['workers'], plan['policy'])] += 1
    for row in rows:
        grouped[(row['project'], row['workers'], row['policy'])].append(row)
    groups = [{'project': key[0], 'workers': key[1], 'policy': key[2],
               'target_n': planned[key], **summarize(grouped[key])} for key in sorted(planned)]
    complete = len(rows) == len(manifest['schedule'])
    summary = {'state': 'complete' if complete else 'interim',
               'evidence_archive': archive.name, 'archive_sha256': digest,
               'raw_evidence_committed': False,
               'planned_runs': len(manifest['schedule']), 'observed_runs': len(rows),
               'missing_indices': sorted(set(p['index'] for p in manifest['schedule']) - set(r['index'] for r in rows)),
               'fixed_budget': manifest['fixed_budget'], 'protocol_sha256': manifest['protocol_sha256'],
               'prices_cny_per_million': PRICES, 'actual_invoice_fee': None,
               'overall': summarize(rows), 'groups': groups, 'runs': rows,
               'by_workers': [{'workers': workers, **summarize([r for r in rows if r['workers'] == workers])}
                              for workers in sorted({r['workers'] for r in rows})],
               'handoff_separate': handoff, 'gui_smoke_separate': gui_smoke,
               'definitions': {'ready_wait': 'first Worker running minus latest dependency terminal completion; baseline uses job.created. Dynamic review is created by validate completion, gate waits for validate+review. Includes controller/launch delay, not pure resource queue time.',
                   'cpu': 'sum of terminal Worker cgroup cpu.stat usage_usec, across executed attempts; excludes control process/model broker/cloud CPU',
                   'memory': 'max of history.memory_bytes sums sampled active Worker cgroup memory.current; not a true simultaneous peak, excludes broker/control/cloud memory',
                   'cost': 'provider usage estimate: cache miss 2, hit .04, output 8 CNY/million; unspecified cache input treated as miss; all-input-miss upper estimate also reported; no invoice reconciliation'},
               'limits': ['Only three repetitions per cell; no significance or causal performance claims.',
                          'Sequential interleaving, but cloud load, output length and provider prefix-cache state remain uncontrolled.',
                          'Tiny DAG offers at most analyze/test_plan concurrency and little resource competition; cannot establish scheduling-policy advantage.',
                          'Failed runs stop earlier; report all-run and success-only wall time separately.',
                          'Fixed fixtures and immutable tests verify controlled repair, not arbitrary repository repair or security isolation.']}
    round_usage = Counter(summary['overall']['usage'])
    round_usage.update(handoff_usage)
    round_usage.update(gui_smoke['usage'])
    summary['round_total'] = {'model_calls': summary['overall']['model_calls'] + handoff_calls + gui_smoke['model_calls'],
                              'usage': dict(round_usage), **estimate(round_usage)}
    output.parent.mkdir(parents=True, exist_ok=True)
    (output.parent / 'manifest.json').write_text(json.dumps({**manifest, 'raw_archive_sha256': digest,
        'raw_archive_name': archive.name, 'raw_evidence_committed': False}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    status = '完整观测' if complete else '临时结果'
    total = summary['overall']
    lines = ['# 网页修复与三项目重复实验（2026-11-11）', '',
             f"{status}：已收集 {len(rows)}/{len(manifest['schedule'])} 次，成功 {total['success_count']} 次、失败 {total['failure_count']} 次。数据只能支持受控修复流程已实际执行，不能据此宣称性能优势。", '',
             '## 网页工程闭环', '',
             '控制台提供三个 Python 修复项目。实际顺序为：原版失败 → 分析与测试建议（可并行）→ 生成补丁 → AST 检查与不可变公开/隐藏测试 → 独立评审 → 校验评审结论及源码 SHA256。测试不通过时停止后续评审；原版红、修复绿与最终接受分别展示。', '',
             '三个项目分别覆盖整数金额与舍入、半开时间区间与时间桶/TTL、Unicode 空白及文本规范化。它们是固定小项目，不是任意仓库上传执行器。', '',
             '## 实验设计与可追溯证据', '',
             f"3 项目 × 2 并发数（1/2）× 3 策略（fifo/resource_fit/aging_fit）× 3 次重复 = 54 次；按照 manifest.schedule 交错顺序逐个提交，失败不补跑。所有配置固定预算：{manifest['fixed_budget']['cpu_milli']} millicpu、1 GiB 内存、{manifest['fixed_budget']['pids']} pids。", '',
             f"原始归档 `{archive.name}` 仅保留在本地 output，不提交 Git；内容 SHA256：`{digest}`。协议 SHA256：`{manifest['protocol_sha256']}`。逐次来源为归档 `{PREFIX}runs/NNN/metrics.json` 与 `detail.json`。", '',
             '仓库内可复算摘要：[summary.json](../validation/projects-2026-11-11/summary.json)；调度及来源：[manifest.json](../validation/projects-2026-11-11/manifest.json)。在仓库根目录重跑：`python -X utf8 tools/analyze_project_benchmark.py --archive ../output/repair-evidence.zip`。摘要严格选择指标，不复制提示词、密钥、源码或模型正文。', '',
             '## 分组结果', '',
             '表内耗时为秒，格式为中位数 [最小值, 最大值]；成功列仅统计最终被接受的运行。失败提前结束，不能把全样本耗时更短解释为更快完成任务。', '',
             '|项目|并发|策略|n/计划|成功/失败|全样本耗时|成功耗时|估算费用/元|',
             '|---|---:|---|---:|---:|---|---|---:|']
    def show(s):
        return '—' if not s['n'] else f"{s['median']:.3f} [{s['min']:.3f}, {s['max']:.3f}]"
    for group in groups:
        lines.append(f"|{group['project']}|{group['workers']}|{group['policy']}|{group['n']}/{group['target_n']}|{group['success_count']}/{group['failure_count']}|{show(group['wall_seconds_all'])}|{show(group['wall_seconds_success_only'])}|{group['estimated_cny']:.6f}|")
    lines += ['', '按并发数汇总只作样本描述，各项目/策略混合后的分布不能用于因果推断。', '',
              '|并发数|n|成功/失败|全样本耗时|成功耗时|就绪等待和/秒|Worker CPU/秒|',
              '|---:|---:|---:|---|---|---|---|']
    for group in summary['by_workers']:
        lines.append(f"|{group['workers']}|{group['n']}|{group['success_count']}/{group['failure_count']}|{show(group['wall_seconds_all'])}|{show(group['wall_seconds_success_only'])}|{show(group['ready_wait_sum_seconds'])}|{show(group['worker_cpu_seconds'])}|")
    lines += ['', '## 等待、资源、token 与费用口径', '',
              f"已观测运行合计调用 {total['model_calls']} 次；token：`{json.dumps(total['usage'], ensure_ascii=False)}`。费用按本次指定费率（每百万输入未命中 2 元、命中 0.04 元、输出 8 元）估算为 **{total['estimated_cny']:.6f} 元**，所有输入均按未命中计为 {total['all_input_miss_upper_cny']:.6f} 元。未与供应商账单核对，实际费用未知。", '',
              f"另列凭据迁移 handoff 验证：{handoff_calls} 次调用、{handoff_usage.get('total_tokens', 0)} token、估算 {summary['handoff_separate']['estimated_cny']:.6f} 元，不计入 54 次实验。", '',
              f"另列 GUI 一键修复 smoke：{gui_smoke['model_calls']} 次调用、{gui_smoke['usage'].get('total_tokens', 0)} token、估算 {gui_smoke['estimated_cny']:.6f} 元。整轮（矩阵 + handoff + GUI）合计 {summary['round_total']['model_calls']} 次调用、{round_usage.get('total_tokens', 0)} token、估算 {summary['round_total']['estimated_cny']:.6f} 元。", '',
              '以上采用高峰费率保守估算，并非实际账单。[DeepSeek 官方定价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)区分峰谷时段；本轮实验在 18:22 后运行，仍统一以高峰费率估计，没有把低谷折扣作为实际已支付金额。此处未做汇率折算，最终金额以服务商账单为准。', '',
              '就绪等待按真实 Worker 事件计算：任务首次 running 时间减去依赖最晚完成时间；baseline 采用作业创建时间。动态 review 从 validate 完成开始计，gate 从 validate/review 都完成开始计。此值包括控制器与进程启动开销，不能称为纯资源排队时间。逐任务等待与每次运行的等待和均保存在 JSON。', '',
              f"每次运行的就绪等待总和：{show(total['ready_wait_sum_seconds'])} 秒。Worker CPU 时间：{show(total['worker_cpu_seconds'])} 秒，来自终态 cgroup cpu.stat 的 usage_usec 累加；不含控制器、模型代理或云端模型 CPU。", '',
              f"每次运行的最大内存采样值：{show(total['max_sampled_worker_memory_bytes'])} 字节。它取 history 内各活动 Worker 的 memory.current 采样总和最大值，**不是实际同时峰值**；也不包含控制器、代理及云端内存。", '',
              '## 失败与限制', '',
              f"失败分类：`{json.dumps(total['failure_classes'], ensure_ascii=False)}`。策略/AST 拒绝依据 validation.error 分类；安全但不正确的补丁依据公开/隐藏测试失败分类。原始错误文字与源码保留在受限原始证据，不进入此摘要。", '',
              '每格仅 n=3，且模型服务负载、回复长度、供应商前缀缓存会影响耗时和费用；交错执行不能消除这些影响。这个 DAG 只有分析与测试建议两个并行分支，几乎没有资源竞争，不足以检验复杂调度场景的优势。只报告观测，不做显著性、因果或性能优势推断。', '',
              ('54 次已经收齐。本报告仍只描述本轮样本，不保证未来成功率。' if complete else '当前尚未收齐 54 次；所有表格和总计均为临时结果。归档更新后重跑同一脚本即可刷新，不应追加付费调用。'), '']
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'state': summary['state'], 'runs': len(rows), 'success': total['success_count'],
                      'failure': total['failure_count'], 'estimated_cny': total['estimated_cny']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'validation/projects-2026-11-11/summary.json')
    parser.add_argument('--document', type=Path, default=ROOT / 'docs/网页修复与三项目重复实验-2026-11-11.md')
    args = parser.parse_args()
    generate(args.archive, args.output, args.document)
