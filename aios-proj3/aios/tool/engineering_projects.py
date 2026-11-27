"""Specifications for vetted engineering fixtures adapted from external projects."""
import json
from pathlib import Path

FIXTURES = Path(__file__).with_name('fixtures')


def json_pointer_project():
    root = FIXTURES / 'json_pointer'
    signatures = {
        'pointer_codec.py': {'escape': ('s',), 'unescape': ('s',)},
        'pointer_path.py': {'join_pointer': ('parts',), 'split_pointer': ('path',)},
    }
    files = {name: (root / name).read_text(encoding='utf-8') for name in signatures}
    tests = {name: (root / name).read_text(encoding='utf-8')
             for name in ('tests/test_public.py', 'tests/test_hidden.py')}
    reference = {
        'pointer_codec.py': files['pointer_codec.py'].replace(
            "s.replace('~0', '~').replace('~1', '/')", "s.replace('~1', '/').replace('~0', '~')"
        ),
        'pointer_path.py': files['pointer_path.py'].replace(
            "return '/' + '/'.join(escape(part) for part in parts)",
            "return '/' + '/'.join(escape(part) for part in parts) if parts else ''"
        ).replace("path[1:].split('/') if part", "path[1:].split('/')"),
    }
    return {
        'name': 'python-json-pointer v3.0.0 · 受控上游样本',
        'file': 'pointer_codec.py + pointer_path.py',
        'multi_file': True, 'editable_files': tuple(files), 'signatures': signatures,
        'allowed_imports': {'pointer_path.py': {'pointer_codec'}},
        'files': files, 'source': json.dumps(files, ensure_ascii=False, indent=2),
        'tests': tests, 'reference_fix_files': reference,
        'reference_fix': json.dumps(reference, ensure_ascii=False, indent=2),
        'provenance': json.loads((root / 'provenance.json').read_text(encoding='utf-8')),
        'license_text': (root / 'LICENSE.txt').read_text(encoding='utf-8'),
        'requirements': (
            '按 JSON Pointer 规则对 ~ 和 / 编码，并且每个转义序列只解码一次。'
            '没有路径分段时返回空字符串。单个空键表示为 /，连续与末尾空键也须保留。'
            '非空指针必须以 / 开头。保持公开函数签名，只修改两个源码文件。'
        ),
    }
