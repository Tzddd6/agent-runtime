# Project module contract / 项目模块说明
# 职责：定义受控工具与固定工程；AST限制不等价于任意代码安全沙箱。
# 主要接口：manifest, digest, validate_files, check_report, run_check。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
"""Bounded multi-file repair fixture; not an arbitrary repository executor.

Only fixed source paths, imports and pure function syntax are accepted. The
caller must execute this checker through ProcessService for OS isolation.
"""
import ast
from hashlib import sha256
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


PROJECT = 'order_project'
REQUIREMENTS = ('All amounts are integer cents. Apply percentage discount to the '
                'whole line then floor the discounted total once; do not floor the discount amount separately. Shipping is free at subtotal >=5000, '
                'otherwise 599. total_cents includes discounted subtotal and shipping. '
                'Negative prices/quantities and discounts outside 0..100 raise ValueError.')
SOURCE = {
    'order_app/pricing.py': '''def line_total_cents(unit_cents, quantity, discount_pct):
    if unit_cents < 0 or quantity < 0 or discount_pct < 0 or discount_pct > 100:
        raise ValueError("invalid")
    return unit_cents * quantity - discount_pct
''',
    'order_app/shipping.py': '''def shipping_cents(subtotal_cents):
    if subtotal_cents < 0:
        raise ValueError("invalid")
    if subtotal_cents > 5000:
        return 0
    return 599
''',
    'order_app/orders.py': '''from order_app.pricing import line_total_cents
from order_app.shipping import shipping_cents

def total_cents(unit_cents, quantity, discount_pct):
    subtotal = line_total_cents(unit_cents, quantity, discount_pct)
    return subtotal
''',
}
SIGNATURES = {
    'order_app/pricing.py': ('line_total_cents', ('unit_cents', 'quantity', 'discount_pct')),
    'order_app/shipping.py': ('shipping_cents', ('subtotal_cents',)),
    'order_app/orders.py': ('total_cents', ('unit_cents', 'quantity', 'discount_pct')),
}
# Evaluator-owned tests are never included in model prompts or inspect output.
TEST_SOURCE = '''import unittest
from order_app.pricing import line_total_cents
from order_app.shipping import shipping_cents
from order_app.orders import total_cents

class OrderTests(unittest.TestCase):
    def test_percentage_floor_once(self): self.assertEqual(line_total_cents(199, 3, 10), 537)
    def test_discount(self): self.assertEqual(line_total_cents(250, 4, 25), 750)
    def test_zero_quantity(self): self.assertEqual(line_total_cents(199, 0, 10), 0)
    def test_full_discount(self): self.assertEqual(line_total_cents(250, 4, 100), 0)
    def test_negative_price(self):
        with self.assertRaises(ValueError): line_total_cents(-1, 1, 0)
    def test_negative_quantity(self):
        with self.assertRaises(ValueError): line_total_cents(1, -1, 0)
    def test_invalid_discount(self):
        with self.assertRaises(ValueError): line_total_cents(1, 1, 101)
    def test_shipping_boundary(self): self.assertEqual(shipping_cents(5000), 0)
    def test_shipping_below(self): self.assertEqual(shipping_cents(4999), 599)
    def test_negative_subtotal(self):
        with self.assertRaises(ValueError): shipping_cents(-1)
    def test_order(self): self.assertEqual(total_cents(199, 3, 10), 1136)
    def test_free_order(self): self.assertEqual(total_cents(2500, 2, 0), 5000)
    def test_discount_threshold(self): self.assertEqual(total_cents(2500, 2, 1), 5549)
'''
RUNNER = '''import json, unittest, os
from pathlib import Path
suite = unittest.defaultTestLoader.discover('tests', top_level_dir='.')
result = unittest.TextTestRunner(verbosity=2).run(suite)
with open('_test_result.json', 'w', encoding='utf-8') as f:
    json.dump({'tests_run': result.testsRun, 'failures': len(result.failures),
               'errors': len(result.errors), 'skipped': len(result.skipped),
               'success': result.wasSuccessful(), 'test_pid': os.getpid(),
               'test_cgroup': Path('/proc/self/cgroup').read_text().strip() if Path('/proc/self/cgroup').exists() else None}, f)
raise SystemExit(0 if result.wasSuccessful() else 1)
'''


def manifest():
    return {'project_id': PROJECT, 'project': '多文件订单工程修复',
            'requirements': REQUIREMENTS, 'files': dict(SOURCE),
            'editable_paths': sorted(SOURCE), 'test_policy': 'evaluator-owned unittest; no test edits'}


def digest(files):
    return sha256(json.dumps(files, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode('utf-8')).hexdigest()


def validate_files(files):
    if not isinstance(files, dict) or set(files) != set(SOURCE):
        raise ValueError('Supply exactly the three editable source files; tests and paths are immutable')
    safe_nodes = (ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return,
                  ast.If, ast.Raise, ast.Assign, ast.Name, ast.Load, ast.Store,
                  ast.Constant, ast.Expr, ast.Call, ast.BinOp, ast.UnaryOp,
                  ast.BoolOp, ast.Compare, ast.Add, ast.Sub, ast.Mult, ast.FloorDiv,
                  ast.Mod, ast.USub, ast.UAdd, ast.And, ast.Or, ast.Not,
                  ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
                  ast.ImportFrom, ast.alias)
    for path, text in files.items():
        if not isinstance(text, str) or not 0 < len(text.encode('utf-8')) <= 16000:
            raise ValueError('Invalid source size')
        tree = ast.parse(text, filename=path)
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
        name, args = SIGNATURES[path]
        if len(functions) != 1 or functions[0].name != name:
            raise ValueError('Exactly the original function must be supplied')
        function = functions[0]
        if (tuple(a.arg for a in function.args.args) != args or function.args.posonlyargs
                or function.args.vararg or function.args.kwarg or function.args.kwonlyargs
                or function.args.defaults or function.decorator_list or function.returns):
            raise ValueError('Function signature must be preserved')
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.ImportFrom)):
                raise ValueError('No executable top-level statements')
        for node in ast.walk(tree):
            if not isinstance(node, safe_nodes):
                raise ValueError('Unsupported syntax: ' + type(node).__name__)
            if isinstance(node, ast.Name) and node.id.startswith('_'):
                raise ValueError('Private names forbidden')
            if isinstance(node, ast.ImportFrom):
                allowed = {'order_app.pricing': 'line_total_cents', 'order_app.shipping': 'shipping_cents'}
                if (path != 'order_app/orders.py' or node.level or node.module not in allowed
                        or len(node.names) != 1 or node.names[0].name != allowed[node.module]
                        or node.names[0].asname):
                    raise ValueError('Import outside fixed project dependencies')
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in {
                        'ValueError', 'min', 'max', 'line_total_cents', 'shipping_cents'}:
                    raise ValueError('Call target forbidden')
    return dict(files)


def check_report(report, returncode):
    return (type(report.get('tests_run')) is int and report['tests_run'] == 13
            and report.get('failures') == 0 and report.get('errors') == 0
            and report.get('skipped') == 0 and report.get('success') is True
            and returncode == 0)


def run_check(files):
    files = validate_files(files)
    with TemporaryDirectory(prefix='aios-order-check-') as directory:
        root = Path(directory)
        for path, text in {**files, 'order_app/__init__.py': '', 'tests/__init__.py': '',
                           'tests/test_orders.py': TEST_SOURCE, '_runner.py': RUNNER}.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')
        environment = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'}
        if os.name == 'nt' and os.environ.get('SystemRoot'):
            environment['SystemRoot'] = os.environ['SystemRoot']
        with (root / 'test.log').open('w+', encoding='utf-8') as log:
            process = subprocess.run([sys.executable, '-B', '_runner.py'], cwd=root,
                                     env=environment, stdout=log, stderr=subprocess.STDOUT,
                                     timeout=6, check=False)
            log.seek(0)
            transcript = log.read(48000)
        transcript = transcript.replace(str(root), '<workspace>')
        transcript = re.sub(r'Ran (\d+) tests in [0-9.]+s', r'Ran \1 tests', transcript)
        report = json.loads((root / '_test_result.json').read_text(encoding='utf-8'))
        return {**report, 'success': check_report(report, process.returncode),
                'files': files, 'source_sha256': digest(files), 'test_log': transcript,
                'test_suite_sha256': sha256(TEST_SOURCE.encode()).hexdigest(),
                'test_engine': 'python_unittest_subprocess', 'returncode': process.returncode}
