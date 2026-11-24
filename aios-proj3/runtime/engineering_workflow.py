# Project module contract / 项目模块说明
# 职责：连接常驻内核、工作流与网页；业务成功必须由真实执行结果确认。
# 主要接口：patch_messages, decode_patch, build。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
"""Real multi-file unittest repair routed through the existing Worker service."""
from hashlib import sha256
import io
import json
import re
from zipfile import ZipFile, ZIP_DEFLATED

from aios.tool import order_project
from cerebrum.tool.apis import ToolQuery


def patch_messages(files, feedback):
    """Shared prompt contract for Runtime and direct orchestration comparisons."""
    spec = order_project.manifest()
    payload = {'requirements': spec['requirements'], 'editable_paths': spec['editable_paths'],
               'files': files, 'test_feedback': feedback}
    return [{'role': 'system', 'content':
             'Repair this fixed Python package. Only modify the three listed source files. '
             'Preserve function signatures, use pure integer arithmetic, and only import '
             'the existing order_app dependencies. Do not change tests. '
             'Return one JSON object {"files":{"relative/path.py":"complete source"}}.'},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, sort_keys=True)}]


def decode_patch(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 128*1024:
        raise ValueError('Patch output exceeds limit')
    # Accept one complete patch object, including one wrapped in commentary or
    # Markdown. Never eval text, repair truncated JSON, or choose between patches.
    decoder = json.JSONDecoder()
    candidates = []
    starts = list(re.finditer(r'\{', raw))
    if len(starts) > 1000:
        raise ValueError('Too many JSON candidates')
    for match in starts:
        try:
            value, _ = decoder.raw_decode(raw, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and set(value) == {'files'}:
            candidates.append(value['files'])
    if len(candidates) != 1:
        raise ValueError('Expected exactly one complete patch object')
    return order_project.validate_files(candidates[0])


def build(console, workflow, model):
    kernel = console.kernel
    spec = order_project.manifest()
    spec.update(file='order-project.zip', source=json.dumps(spec['files'], ensure_ascii=False, indent=2))
    agents = {role: 'order-' + workflow + '-' + role for role in ('analysis', 'repair', 'testing', 'review', 'delivery')}
    spec['role_agents'] = list(agents.values())
    for name in agents.values():
        kernel.scheduler.drf_controller.register_agent(name, max_model_calls=2, max_tokens=16384)

    def test(files):
        response = kernel.execute_request(agents['testing'], ToolQuery(
            tool_calls=[{'name': 'project.workspace_check', 'parameters': {'files': files}}],
            resources={'cpu_milli': 500, 'memory_bytes': 128 * 1024**2, 'pids': 16},
            execution_timeout=10))['response']
        if response.error or response.status_code != 200:
            raise RuntimeError('Isolated project test did not complete')
        return response.response_message

    def baseline(*_):
        checked = test(spec['files'])
        if checked['success'] or checked['tests_run'] != 13 or not checked['failures']:
            raise RuntimeError('Baseline must demonstrate actual failing tests')
        return checked

    def patch(task, upstream, attempt):
        previous = upstream.get('verify')
        # Evaluator test source, expected solutions and retained oracle cases are
        # not sent to the model. The observed public execution log is feedback.
        raw = model(patch_messages(previous['files'] if previous else spec['files'],
                                  previous['test_log'] if previous else upstream['plan']['test_feedback']),
                    4096, agent_name=agents['repair'])
        return {'files': decode_patch(raw)}

    def verify(task, upstream, attempt):
        checked = test(upstream['patch']['files'])
        if not checked['success']:
            checked['spawn_tasks'] = [
                {'id': 'patch_round_2', 'agent': agents['repair'], 'handler': patch,
                 'resources': {'cpu_milli': 0, 'memory_bytes': 0, 'pids': 0}},
                {'id': 'verify_round_2', 'agent': agents['testing'], 'depends_on': ['patch_round_2'],
                 'wire_into': ['review'], 'handler': lambda t, up, a: test(up['patch_round_2']['files']),
                 'resources': {'cpu_milli': 0, 'memory_bytes': 0, 'pids': 0}}]
        return checked

    def review(task, upstream, attempt):
        checked = upstream.get('verify_round_2') or upstream['verify']
        if (not checked['success'] or checked['tests_run'] != 13 or checked['skipped']
                or order_project.digest(checked['files']) != checked['source_sha256']):
            raise RuntimeError('Project verification failed; no success artifact will be published')
        return checked

    def deliver(task, upstream, attempt):
        checked = upstream['review']
        buffer = io.BytesIO()
        with ZipFile(buffer, 'w', ZIP_DEFLATED) as archive:
            for name, text in sorted({**checked['files'], 'order_app/__init__.py': '',
                                     'tests/__init__.py': '', 'tests/test_orders.py': order_project.TEST_SOURCE}.items()):
                archive.writestr(name, text)
        raw = buffer.getvalue()
        with ZipFile(io.BytesIO(raw)) as archive:
            readback = {name: archive.read(name).decode('utf-8') for name in spec['editable_paths']}
        if order_project.digest(readback) != checked['source_sha256']:
            raise RuntimeError('Artifact source differs from tested source')
        directory = console.root / 'artifacts'
        directory.mkdir(exist_ok=True)
        target = directory / (workflow + '.zip')
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes(raw)
        temporary.replace(target)
        return {**checked, 'source': json.dumps(checked['files'], ensure_ascii=False, indent=2),
                'agents': agents, 'artifact': {'path': 'artifacts/' + target.name,
                'sha256': sha256(raw).hexdigest(), 'source_sha256': checked['source_sha256'],
                'readback_verified': True, 'download_url': '/api/jobs/' + workflow + '/artifact'}}

    tasks = [
        {'id': 'inspect', 'agent': agents['analysis'], 'handler': lambda *_: spec},
        {'id': 'baseline', 'agent': agents['testing'], 'depends_on': ['inspect'], 'handler': baseline},
        {'id': 'plan', 'agent': agents['analysis'], 'depends_on': ['baseline'], 'handler': lambda t, up, a: {
            'requirements': spec['requirements'], 'test_feedback': up['baseline']['test_log']}},
        {'id': 'patch', 'agent': agents['repair'], 'depends_on': ['plan'], 'handler': patch},
        {'id': 'verify', 'agent': agents['testing'], 'depends_on': ['patch'], 'handler': verify},
        {'id': 'review', 'agent': agents['review'], 'depends_on': ['verify'], 'handler': review},
        {'id': 'answer', 'agent': agents['delivery'], 'depends_on': ['review'], 'handler': deliver}]
    return spec, tasks
