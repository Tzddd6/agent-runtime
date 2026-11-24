# Project module contract / 项目模块说明
# 职责：定义受控工具与固定工程；AST限制不等价于任意代码安全沙箱。
# 主要接口：canonical_project, manifest, validate_patch, run_check。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
"""
Three Controlled Python Engineering Repair Projects for AIOS Kernel (pricing, intervals, text_normalization).
Includes AST subset checker, baseline defect verification, reference patch verification, and 7-stage repair workflow.
"""
import ast
import hashlib
import json
from typing import Any, Dict, List, Tuple


PROJECTS: Dict[str, Dict[str, Any]] = {
    "pricing": {
        "name": "金额计算 · 折扣、免邮与退款",
        "file": "pricing.py",
        "signatures": {
            "line_total_cents": ("unit_cents", "quantity", "discount_pct"),
            "shipping_cents": ("subtotal_cents",),
            "refund_cents": ("paid_cents", "returned_qty", "total_qty"),
        },
        "source": (
            "def line_total_cents(unit_cents, quantity, discount_pct):\n"
            "    if unit_cents < 0 or quantity < 0 or discount_pct < 0 or discount_pct > 100:\n"
            '        raise ValueError("invalid")\n'
            "    return unit_cents * quantity - discount_pct\n\n"
            "def shipping_cents(subtotal_cents):\n"
            "    if subtotal_cents < 0:\n"
            '        raise ValueError("invalid")\n'
            "    if subtotal_cents > 5000:\n"
            "        return 0\n"
            "    return 599\n\n"
            "def refund_cents(paid_cents, returned_qty, total_qty):\n"
            "    if paid_cents < 0 or total_qty <= 0 or returned_qty < 0 or returned_qty > total_qty:\n"
            '        raise ValueError("invalid")\n'
            "    return paid_cents // total_qty * returned_qty\n"
        ),
        "reference_fix": (
            "def line_total_cents(unit_cents, quantity, discount_pct):\n"
            "    if unit_cents < 0 or quantity < 0 or discount_pct < 0 or discount_pct > 100:\n"
            '        raise ValueError("invalid")\n'
            "    return (unit_cents * quantity * (100 - discount_pct)) // 100\n\n"
            "def shipping_cents(subtotal_cents):\n"
            "    if subtotal_cents < 0:\n"
            '        raise ValueError("invalid")\n'
            "    if subtotal_cents >= 5000:\n"
            "        return 0\n"
            "    return 599\n\n"
            "def refund_cents(paid_cents, returned_qty, total_qty):\n"
            "    if paid_cents < 0 or total_qty <= 0 or returned_qty < 0 or returned_qty > total_qty:\n"
            '        raise ValueError("invalid")\n'
            "    return (paid_cents * returned_qty) // total_qty\n"
        ),
        "requirements": (
            "line_total_cents: apply percentage discount_pct (0..100) to whole line and floor once. "
            "shipping_cents: free shipping (0) for subtotal_cents >= 5000, else 599. "
            "refund_cents: prorate whole paid_cents * returned_qty // total_qty and floor once."
        ),
        "public": [
            ("line_total_cents", (199, 3, 10), 537),
            ("shipping_cents", (5000,), 0),
            ("refund_cents", (100, 1, 3), 33),
        ],
        "heldout": [
            ("line_total_cents", (250, 4, 25), 750),
            ("shipping_cents", (4999,), 599),
            ("refund_cents", (100, 2, 3), 66),
        ],
    },
    "intervals": {
        "name": "时间区间 · 相交、分桶与 TTL",
        "file": "intervals.py",
        "signatures": {
            "overlap_seconds": ("start_a", "end_a", "start_b", "end_b"),
            "bucket_floor": ("timestamp", "window"),
            "is_expired": ("issued_at", "ttl", "now"),
        },
        "source": (
            "def overlap_seconds(start_a, end_a, start_b, end_b):\n"
            "    if end_a < start_a or end_b < start_b:\n"
            '        raise ValueError("invalid")\n'
            "    return min(end_a, end_b) - max(start_a, start_b)\n\n"
            "def bucket_floor(timestamp, window):\n"
            "    if timestamp < 0 or window <= 0:\n"
            '        raise ValueError("invalid")\n'
            "    return timestamp // window\n\n"
            "def is_expired(issued_at, ttl, now):\n"
            "    if ttl < 0:\n"
            '        raise ValueError("invalid")\n'
            "    return now > issued_at + ttl\n"
        ),
        "reference_fix": (
            "def overlap_seconds(start_a, end_a, start_b, end_b):\n"
            "    if end_a < start_a or end_b < start_b:\n"
            '        raise ValueError("invalid")\n'
            "    return max(0, min(end_a, end_b) - max(start_a, start_b))\n\n"
            "def bucket_floor(timestamp, window):\n"
            "    if timestamp < 0 or window <= 0:\n"
            '        raise ValueError("invalid")\n'
            "    return (timestamp // window) * window\n\n"
            "def is_expired(issued_at, ttl, now):\n"
            "    if ttl < 0:\n"
            '        raise ValueError("invalid")\n'
            "    return now >= issued_at + ttl\n"
        ),
        "requirements": (
            "overlap_seconds: return non-negative overlap duration (0 when disjoint). "
            "bucket_floor: align timestamp down to multiple of window. "
            "is_expired: true when now >= issued_at + ttl."
        ),
        "public": [
            ("overlap_seconds", (0, 5, 10, 15), 0),
            ("bucket_floor", (125, 60), 120),
            ("is_expired", (100, 30, 130), True),
        ],
        "heldout": [
            ("overlap_seconds", (2, 10, 5, 8), 3),
            ("bucket_floor", (59, 60), 0),
            ("is_expired", (100, 30, 129), False),
        ],
    },
    "text_normalization": {
        "name": "文本规范化 · 空白、词数与前缀",
        "file": "text_normalization.py",
        "signatures": {
            "normalize_spaces": ("text",),
            "word_count": ("text",),
            "has_prefix_ci": ("text", "prefix"),
        },
        "source": (
            "def normalize_spaces(text):\n"
            "    return text.strip()\n\n"
            "def word_count(text):\n"
            '    return len(text.split(" "))\n\n'
            "def has_prefix_ci(text, prefix):\n"
            "    return text.lower().startswith(prefix.lower())\n"
        ),
        "reference_fix": (
            "def normalize_spaces(text):\n"
            '    return " ".join(text.strip().split())\n\n'
            "def word_count(text):\n"
            "    return len(text.strip().split())\n\n"
            "def has_prefix_ci(text, prefix):\n"
            "    return text.strip().lower().startswith(prefix.strip().lower())\n"
        ),
        "requirements": (
            "normalize_spaces: collapse consecutive whitespace to single spaces and strip ends. "
            "word_count: return count of whitespace-delimited words (0 for blank string). "
            "has_prefix_ci: case-insensitive prefix match after stripping leading/trailing whitespace from both text and prefix."
        ),
        "public": [
            ("normalize_spaces", ("  hello   world  ",), "hello world"),
            ("word_count", ("   ",), 0),
            ("has_prefix_ci", ("  AIOS Kernel", "aios"), True),
        ],
        "heldout": [
            ("normalize_spaces", ("a\t\tb\nc",), "a b c"),
            ("word_count", ("one   two  three",), 3),
            ("has_prefix_ci", ("  openEuler", "  OPEN"), True),
        ],
    },
}


def canonical_project(project: str) -> str:
    aliases = {
        "integer-pricing": "pricing",
        "time-intervals": "intervals",
        "text-normalization": "text_normalization",
    }
    p = aliases.get(project, project)
    if p not in PROJECTS:
        raise ValueError(f"Unknown controlled project: {project}")
    return p


def manifest(project: str = "pricing") -> Dict[str, Any]:
    key = canonical_project(project)
    spec = PROJECTS[key]
    return {
        "project_id": key,
        "project": spec["name"],
        "file": spec["file"],
        "requirements": spec["requirements"],
        "source": spec["source"],
        "reference_fix": spec["reference_fix"],
    }


def validate_patch(raw: Any, project: str = "pricing") -> str:
    key = canonical_project(project)
    spec = PROJECTS[key]
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
            source = obj["source"] if isinstance(obj, dict) and "source" in obj else raw
        except Exception:
            source = raw
    elif isinstance(raw, dict) and "source" in raw:
        source = raw["source"]
    else:
        raise ValueError("Patch must provide source string")

    source = str(source).strip() + "\n"
    tree = ast.parse(source, filename=spec["file"])
    if not all(isinstance(node, ast.FunctionDef) for node in tree.body):
        raise ValueError("Controlled patches may contain function definitions only")
    allowed_calls = set(spec['signatures']) | {'ValueError', 'min', 'max', 'len'}
    allowed_methods = {'strip', 'split', 'lower', 'startswith', 'join', 'replace', 'casefold', 'endswith'}
    for function in tree.body:
        if function.decorator_list or function.returns or function.args.defaults or function.args.kw_defaults:
            raise ValueError("Decorators, annotations and default arguments are not supported")
        if tuple(arg.arg for arg in function.args.args) != spec['signatures'].get(function.name):
            raise ValueError("Function signature mismatch")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith('_'):
            raise ValueError("Private names are forbidden")
        if isinstance(node, ast.Attribute) and node.attr not in allowed_methods:
            raise ValueError("Attribute access is not allowed")
        if isinstance(node, ast.Call):
            if not ((isinstance(node.func, ast.Name) and node.func.id in allowed_calls) or
                    (isinstance(node.func, ast.Attribute) and node.func.attr in allowed_methods)):
                raise ValueError("Call target is not allowed")
        if isinstance(node, (ast.Lambda, ast.ClassDef, ast.Global, ast.Nonlocal, ast.AsyncFunctionDef)):
            raise ValueError("Unsupported code construct")
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.While, ast.For, ast.Try, ast.With)):
            raise ValueError(f"Forbidden AST construct in controlled patch: {type(node).__name__}")
    fn_names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    if set(fn_names) != set(spec["signatures"].keys()):
        raise ValueError(f"Expected functions {list(spec['signatures'].keys())}, got {fn_names}")
    return source


def _run_suite(namespace: Dict[str, Any], cases: List[Tuple[str, Tuple, Any]]) -> Dict[str, Any]:
    passed = 0
    failed = 0
    details = []
    for fn_name, args, expected in cases:
        try:
            actual = namespace[fn_name](*args)
            ok = actual == expected
        except Exception as exc:
            actual = f"Error: {exc}"
            ok = False
        if ok:
            passed += 1
        else:
            failed += 1
        details.append({"function": fn_name, "args": list(args), "expected": expected, "actual": actual, "ok": ok})
    return {
        "total": len(cases),
        "passed": passed,
        "failed": failed,
        "errors": 0,
        "success": failed == 0,
        "details": details,
    }


def run_check(source: str, project: str = "pricing") -> Dict[str, Any]:
    key = canonical_project(project)
    spec = PROJECTS[key]
    validated = validate_patch({"source": source}, project=key)
    ns: Dict[str, Any] = {"__builtins__": {"ValueError": ValueError, "min": min, "max": max, "len": len}}
    exec(compile(validated, spec["file"], "exec"), ns)  # Safe AST-validated integer/string functions only
    pub = _run_suite(ns, spec["public"])
    held = _run_suite(ns, spec["heldout"])
    digest = hashlib.sha256(validated.encode("utf-8")).hexdigest()
    return {
        "project": key,
        "success": pub["success"] and held["success"],
        "source": validated,
        "source_sha256": digest,
        "public": pub,
        "heldout": held,
        "failing_count": pub["failed"] + held["failed"],
    }
