"""Shared engineering messages and patch parsing for Runtime/control comparisons.

Public fixture analysis is deterministic domain logic, not a separate LLM call.
Hidden tests and reference answers never enter these messages.
Self-authored integration: MIT; relies on aios.tool.engineering's guardrails.
"""
import json
import re
from aios.tool.engineering import sanitize_context_for_model


def stable_context(value):
    value = sanitize_context_for_model(value)
    if isinstance(value, dict):
        return {key: stable_context(item) for key, item in value.items()}
    if isinstance(value, list):
        return [stable_context(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r'order_ws_[A-Za-z0-9_-]+', 'order_ws_<id>', value)
        value = re.sub(r'File "[^"\n]*[/\\]([^"/\\]+\.py)"', r'File "<workspace>/\1"', value)
        return re.sub(r'Ran (\d+) tests? in [0-9.]+s', r'Ran \1 tests in <elapsed>s', value)
    return value


def analysis_plan(project, spec, baseline):
    base = stable_context(baseline)
    return {'agent': 'analyst_agent', 'project': project,
            'multi_file': bool(spec.get('multi_file')),
            'requirements': spec['requirements'],
            'editable_files': spec.get('editable_files', [spec['file']]),
            'baseline_public_failures': base.get('public', {}),
            'root_cause_summary': (
                '跨文件缺陷定位：1. pricing.py 先减优惠券再打折且未限制非负下界；'
                '2. shipping.py 免邮阈值误用 >5000 且在免基础运费时漏收跨区附加费；'
                '3. refunds.py 先整除再相乘导致精度丢失、未退未发货全退运费且缺少剩余可退额度上限。'
                if spec.get('multi_file') else f"单文件缺陷定位：{spec['requirements']}")}


def patch_messages(project, spec, upstream, *, retry=False):
    multi = bool(spec.get('multi_file'))
    if retry:
        prev = stable_context(upstream)
        instruction = {'project': project, 'multi_file': multi,
            'requirements': spec['requirements'],
            'previous_source': prev.get('source', spec['source']),
            'previous_files': prev.get('files'),
            'verification_feedback': {k: v for k, v in prev.items()
                if k not in ('source', 'files', 'spawn_tasks', 'reference_fix', 'test_hashes')}}
        system = (
            '你是严谨的 Python 多文件工程修复代理（patcher_agent）。上一轮补丁未通过独立测试，'
            '请根据失败反馈修复 pricing.py、shipping.py、refunds.py 的全部跨文件边界缺陷，'
            '只输出 JSON 对象 {"files":{"pricing.py":"...","shipping.py":"...","refunds.py":"..."}}。'
            if multi else
            '你是严谨的 Python 工程修复代理。上一轮补丁未通过独立测试，请根据失败反馈修复全部边界缺陷，'
            '严格保持函数签名，只输出 JSON 对象 {"source":"完整Python源码"}，不要输出 Markdown 代码块或其他解释。')
        prefix = '上一轮验证失败，请根据反馈生成第二轮修复源码：'
    else:
        instruction = {'project': project, 'multi_file': multi,
            'source': spec['source'], 'files': spec.get('files'),
            'public_tests': spec.get('public_tests'), 'requirements': spec['requirements'],
            'analysis': upstream}
        system = (
            '你是严谨的 Python 多文件工程修复代理（patcher_agent）。请根据上游 analyst_agent 的根因分析，'
            '同时修复 pricing.py、shipping.py、refunds.py，严格保持原有函数签名，只允许导入白名单模块，'
            '严禁修改 tests/ 目录。只输出 JSON 对象 {"files":{"pricing.py":"...","shipping.py":"...","refunds.py":"..."}}，'
            '不要输出 Markdown 代码块或任何解释。' if multi else
            '你是严谨的 Python 工程修复代理。请严格保持原有函数名和参数签名，覆盖所有边界条件（空串、空白、负数、边界相等），'
            '仅使用纯 Python 实现，只输出 JSON 对象 {"source":"完整Python源码"}，不要输出 Markdown 代码块或任何解释。')
        prefix = '根据需求与基线失败信息修复下面项目：'
    return [{'role': 'system', 'content': system}, {'role': 'user', 'content': prefix +
        json.dumps(stable_context(instruction), ensure_ascii=False, sort_keys=True)}]


def decode_patch(raw, *, multi_file):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 128 * 1024:
        raise ValueError('Invalid patch response size')
    text = raw.strip()
    if text.startswith('```'):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == '```':
            lines.pop()
        text = '\n'.join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if start < 0 or end <= start:
            raise ValueError('Incomplete patch object') from None
        parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError('Patch must be an object')
    if not multi_file:
        if not isinstance(parsed.get('source'), str):
            raise ValueError('Patch must include source text')
        return parsed
    files = parsed.get('files', parsed.get('source'))
    if isinstance(files, str):
        files = json.loads(files)
    if files is None and all(k in parsed for k in ('pricing.py','shipping.py','refunds.py')):
        files = {k: parsed[k] for k in ('pricing.py','shipping.py','refunds.py')}
    if not isinstance(files, dict):
        raise ValueError('Patch must include files mapping')
    return {'files': files, 'source': json.dumps(files, ensure_ascii=False, indent=2)}
