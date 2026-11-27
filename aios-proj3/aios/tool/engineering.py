"""
Controlled Python Engineering Repair Projects for AIOS Kernel (pricing, intervals, text_normalization, order_service).
Includes AST subset checker, multi-file order_service project with cross-file defects, 6 anti-fake-fix guardrails,
isolated disk workspace unittest execution, and deterministic deliverable bundle packaging.
"""
import ast
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple
import zipfile

from aios.tool.engineering_projects import json_pointer_project


ORDER_SERVICE_FILES: Dict[str, str] = {
    "pricing.py": (
        "def line_subtotal_cents(unit_cents, quantity, tier_discount_pct, coupon_cents):\n"
        "    if unit_cents < 0 or quantity <= 0 or tier_discount_pct < 0 or tier_discount_pct > 100 or coupon_cents < 0:\n"
        '        raise ValueError("invalid pricing input")\n'
        "    gross = unit_cents * quantity\n"
        "    # Bug 1: applies coupon before percentage tier discount and allows negative subtotal\n"
        "    discounted = ((gross - coupon_cents) * (100 - tier_discount_pct)) // 100\n"
        "    return discounted\n"
    ),
    "shipping.py": (
        "from pricing import line_subtotal_cents\n\n"
        "def calculate_shipping_cents(subtotal_cents, weight_grams, is_remote):\n"
        "    if subtotal_cents < 0 or weight_grams <= 0:\n"
        '        raise ValueError("invalid shipping input")\n'
        "    # Bug 2: uses strict > 5000 instead of >= 5000 and waives remote surcharge on free base shipping\n"
        "    if subtotal_cents > 5000:\n"
        "        return 0\n"
        "    extra_kg = max(0, (weight_grams - 1000 + 999) // 1000)\n"
        "    base = 500 + extra_kg * 200\n"
        "    return base + (300 if is_remote else 0)\n"
    ),
    "refunds.py": (
        "from pricing import line_subtotal_cents\n"
        "from shipping import calculate_shipping_cents\n\n"
        "def compute_refund_cents(unit_cents, total_qty, returned_qty, tier_discount_pct, coupon_cents, weight_grams, is_remote, shipped, already_refunded_cents):\n"
        "    if returned_qty <= 0 or returned_qty > total_qty or already_refunded_cents < 0:\n"
        '        raise ValueError("invalid refund input")\n'
        "    subtotal = line_subtotal_cents(unit_cents, total_qty, tier_discount_pct, coupon_cents)\n"
        "    shipping = calculate_shipping_cents(subtotal, weight_grams, is_remote)\n"
        "    # Bug 3: integer division before multiplication loses cents, ignores unshipped full-return shipping refund, and lacks cap against remaining refundable amount\n"
        "    item_refund = (subtotal // total_qty) * returned_qty\n"
        "    shipping_refund = 0\n"
        "    return item_refund + shipping_refund\n"
    ),
}

ORDER_SERVICE_REFERENCE_FIX: Dict[str, str] = {
    "pricing.py": (
        "def line_subtotal_cents(unit_cents, quantity, tier_discount_pct, coupon_cents):\n"
        "    if unit_cents < 0 or quantity <= 0 or tier_discount_pct < 0 or tier_discount_pct > 100 or coupon_cents < 0:\n"
        '        raise ValueError("invalid pricing input")\n'
        "    gross = unit_cents * quantity\n"
        "    after_tier = (gross * (100 - tier_discount_pct)) // 100\n"
        "    return max(0, after_tier - coupon_cents)\n"
    ),
    "shipping.py": (
        "from pricing import line_subtotal_cents\n\n"
        "def calculate_shipping_cents(subtotal_cents, weight_grams, is_remote):\n"
        "    if subtotal_cents < 0 or weight_grams <= 0:\n"
        '        raise ValueError("invalid shipping input")\n'
        "    remote_fee = 300 if is_remote else 0\n"
        "    if subtotal_cents >= 5000:\n"
        "        return remote_fee\n"
        "    extra_kg = max(0, (weight_grams - 1000 + 999) // 1000)\n"
        "    base = 500 + extra_kg * 200\n"
        "    return base + remote_fee\n"
    ),
    "refunds.py": (
        "from pricing import line_subtotal_cents\n"
        "from shipping import calculate_shipping_cents\n\n"
        "def compute_refund_cents(unit_cents, total_qty, returned_qty, tier_discount_pct, coupon_cents, weight_grams, is_remote, shipped, already_refunded_cents):\n"
        "    if returned_qty <= 0 or returned_qty > total_qty or already_refunded_cents < 0:\n"
        '        raise ValueError("invalid refund input")\n'
        "    subtotal = line_subtotal_cents(unit_cents, total_qty, tier_discount_pct, coupon_cents)\n"
        "    shipping = calculate_shipping_cents(subtotal, weight_grams, is_remote)\n"
        "    total_paid = subtotal + shipping\n"
        "    if already_refunded_cents > total_paid:\n"
        '        raise ValueError("invalid refund input")\n'
        "    item_refund = (subtotal * returned_qty) // total_qty\n"
        "    shipping_refund = shipping if (not shipped and returned_qty == total_qty) else 0\n"
        "    raw_refund = item_refund + shipping_refund\n"
        "    remaining = max(0, total_paid - already_refunded_cents)\n"
        "    return min(raw_refund, remaining)\n"
    ),
}

ORDER_SERVICE_TESTS: Dict[str, str] = {
    "tests/test_public.py": (
        "import unittest\n"
        "from pricing import line_subtotal_cents\n"
        "from shipping import calculate_shipping_cents\n"
        "from refunds import compute_refund_cents\n\n"
        "class TestOrderServicePublic(unittest.TestCase):\n"
        "    def test_tier_discount_before_coupon_and_free_shipping_threshold(self):\n"
        "        # 3000 * 2 = 6000 -> 10% tier discount = 5400 -> minus 400 coupon = 5000\n"
        "        sub = line_subtotal_cents(3000, 2, 10, 400)\n"
        "        self.assertEqual(sub, 5000)\n"
        "        # Exactly 5000 qualifies for free base shipping, non-remote = 0\n"
        "        self.assertEqual(calculate_shipping_cents(sub, 1500, False), 0)\n\n"
        "    def test_remote_surcharge_and_unshipped_full_refund(self):\n"
        "        sub = line_subtotal_cents(3000, 2, 10, 400)\n"
        "        # Remote surcharge (300) still applies even when subtotal >= 5000\n"
        "        ship = calculate_shipping_cents(sub, 1500, True)\n"
        "        self.assertEqual(ship, 300)\n"
        "        # Full return before shipment refunds both subtotal (5000) and shipping (300)\n"
        "        ref = compute_refund_cents(3000, 2, 2, 10, 400, 1500, True, False, 0)\n"
        "        self.assertEqual(ref, 5300)\n\n"
        "    def test_partial_refund_proration_and_idempotent_cap(self):\n"
        "        # 100 * 3 = 300, no discount/coupon -> 1 returned = 100\n"
        "        self.assertEqual(compute_refund_cents(100, 3, 1, 0, 0, 500, False, True, 0), 100)\n"
        "        # 100 * 1 = 100 -> 10% off = 90 -> 3 items = 270; 100*3 with 33% off = 201; 2 returned = (201*2)//3 = 134\n"
        "        self.assertEqual(compute_refund_cents(100, 3, 2, 33, 0, 500, False, True, 0), 134)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n"
    ),
    "tests/test_hidden.py": (
        "import unittest\n"
        "from pricing import line_subtotal_cents\n"
        "from shipping import calculate_shipping_cents\n"
        "from refunds import compute_refund_cents\n\n"
        "class TestOrderServiceHidden(unittest.TestCase):\n"
        "    def test_coupon_exceeding_discounted_total_floors_at_zero(self):\n"
        "        self.assertEqual(line_subtotal_cents(500, 1, 20, 999), 0)\n"
        "        self.assertEqual(calculate_shipping_cents(0, 2500, False), 900)\n\n"
        "    def test_shipped_full_return_excludes_shipping_and_caps_remaining(self):\n"
        "        # Subtotal = 5000, remote shipping = 300, total_paid = 5300\n"
        "        # Shipped full return only refunds items (5000), not shipping\n"
        "        self.assertEqual(compute_refund_cents(3000, 2, 2, 10, 400, 1500, True, True, 0), 5000)\n"
        "        # If 4800 was already refunded, remaining refundable is capped at 5300 - 4800 = 500\n"
        "        self.assertEqual(compute_refund_cents(3000, 2, 2, 10, 400, 1500, True, True, 4800), 500)\n"
        "        with self.assertRaises(ValueError):\n"
        "            compute_refund_cents(3000, 2, 2, 10, 400, 1500, True, True, 6000)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n"
    ),
}



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
    "order_service": {
        "name": "多文件订单服务 · 计价、运费与按比例幂等退款",
        "file": "order_service/",
        "multi_file": True,
        "editable_files": ("pricing.py", "shipping.py", "refunds.py"),
        "allowed_imports": {
            "pricing.py": set(),
            "shipping.py": {"pricing"},
            "refunds.py": {"pricing", "shipping"},
        },
        "signatures": {
            "pricing.py": {
                "line_subtotal_cents": ("unit_cents", "quantity", "tier_discount_pct", "coupon_cents"),
            },
            "shipping.py": {
                "calculate_shipping_cents": ("subtotal_cents", "weight_grams", "is_remote"),
            },
            "refunds.py": {
                "compute_refund_cents": (
                    "unit_cents",
                    "total_qty",
                    "returned_qty",
                    "tier_discount_pct",
                    "coupon_cents",
                    "weight_grams",
                    "is_remote",
                    "shipped",
                    "already_refunded_cents",
                ),
            },
        },
        "files": ORDER_SERVICE_FILES,
        "tests": ORDER_SERVICE_TESTS,
        "source": json.dumps(ORDER_SERVICE_FILES, ensure_ascii=False, indent=2),
        "reference_fix": json.dumps(ORDER_SERVICE_REFERENCE_FIX, ensure_ascii=False, indent=2),
        "reference_fix_files": ORDER_SERVICE_REFERENCE_FIX,
        "requirements": (
            "1. pricing.py (`line_subtotal_cents`): 先对总额 `unit_cents * quantity` 应用百分比阶梯折扣 `(gross * (100 - tier_discount_pct)) // 100`，"
            "再扣减 `coupon_cents`，且最终商品小计不得低于 0（使用 `max(0, ...)`）。\n"
            "2. shipping.py (`calculate_shipping_cents`): 当折后商品小计 `subtotal_cents >= 5000` 时免除基础运费（0），"
            "否则首重 1000g 内 500 分，每超 1000g（向上取整）加 200 分；但若 `is_remote=True`，无论是否满额免基础运费，均须加收 300 分跨区附加费。\n"
            "3. refunds.py (`compute_refund_cents`): 计算商品退款 `(subtotal * returned_qty) // total_qty`（先乘后整除防丢分）；"
            "仅当未发货 `not shipped` 且全额退货 `returned_qty == total_qty` 时退还运费 `shipping`；"
            "若 `already_refunded_cents > subtotal + shipping` 抛出 `ValueError`，且本次实际退款不得超过剩余可退上限 `max(0, subtotal + shipping - already_refunded_cents)`。"
        ),
    },
}


PROJECTS["json_pointer"] = json_pointer_project()


def canonical_project(project: str) -> str:
    aliases = {
        "integer-pricing": "pricing",
        "time-intervals": "intervals",
        "text-normalization": "text_normalization",
        "order-service": "order_service",
    }
    p = aliases.get(project, project)
    if p not in PROJECTS:
        raise ValueError(f"Unknown controlled project: {project}")
    return p


def compute_tree_sha256(files_map: Dict[str, str]) -> str:
    """Compute canonical SHA-256 digest over a sorted relative-path -> content dictionary."""
    hasher = hashlib.sha256()
    for rel_path in sorted(files_map.keys()):
        norm_path = rel_path.replace("\\", "/").lstrip("/")
        content_bytes = files_map[rel_path].encode("utf-8")
        hasher.update(f"{norm_path}:{len(content_bytes)}\n".encode("utf-8"))
        hasher.update(content_bytes)
        hasher.update(b"\n")
    return hasher.hexdigest()


def manifest(project: str = "pricing", include_reference: bool = True) -> Dict[str, Any]:
    """
    Return project manifest.
    Guardrail #1: `tests/test_hidden.py` is NEVER included in manifest() output,
    and `reference_fix` can be stripped before entering any model context.
    """
    key = canonical_project(project)
    spec = PROJECTS[key]
    out: Dict[str, Any] = {
        "project_id": key,
        "project": spec["name"],
        "file": spec["file"],
        "multi_file": bool(spec.get("multi_file", False)),
        "requirements": spec["requirements"],
        "source": spec["source"],
    }
    if spec.get("multi_file"):
        out["files"] = dict(spec["files"])
        out["editable_files"] = list(spec["editable_files"])
        out["public_tests"] = {"tests/test_public.py": spec["tests"]["tests/test_public.py"]}
    if spec.get("provenance"):
        out["provenance"] = dict(spec["provenance"])
    if include_reference:
        out["reference_fix"] = spec["reference_fix"]
        if spec.get("multi_file"):
            out["reference_fix_files"] = dict(spec["reference_fix_files"])
    return out


def sanitize_context_for_model(payload: Any) -> Any:
    """
    Guardrail #1: Recursively strip `reference_fix`, `reference_fix_files`, `test_hidden.py`,
    and `heldout` from any dictionary/list before constructing a model prompt.
    """
    forbidden_keys = {"reference_fix", "reference_fix_files", "test_hidden", "test_hidden.py", "tests/test_hidden.py", "heldout"}
    if isinstance(payload, dict):
        cleaned = {}
        for k, v in payload.items():
            k_str = str(k)
            if k_str in forbidden_keys or "test_hidden" in k_str:
                continue
            cleaned[k] = sanitize_context_for_model(v)
        return cleaned
    if isinstance(payload, list):
        return [sanitize_context_for_model(item) for item in payload]
    return payload


def _validate_single_module_ast(
    source: str,
    filename: str,
    expected_signatures: Dict[str, Tuple[str, ...]],
    allowed_imports: Optional[set] = None,
    allowed_calls_extra: Optional[set] = None,
) -> str:
    source = str(source).strip() + "\n"
    tree = ast.parse(source, filename=filename)
    allowed_imports = allowed_imports or set()
    imported_funcs: set = set()

    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module not in allowed_imports:
                raise ValueError(f"Disallowed import module '{node.module}' in {filename}")
            for alias in node.names:
                if alias.asname or alias.name.startswith("_"):
                    raise ValueError("Import aliases and private imports are forbidden")
                imported_funcs.add(alias.name)
        elif not isinstance(node, ast.FunctionDef):
            raise ValueError("Controlled patches may contain whitelisted imports and function definitions only")

    allowed_calls = set(expected_signatures) | imported_funcs | {"ValueError", "min", "max", "len", "abs", "bool", "int"}
    if allowed_calls_extra:
        allowed_calls |= set(allowed_calls_extra)
    allowed_methods = {"strip", "split", "lower", "startswith", "join", "replace", "casefold", "endswith"}

    fn_nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    for function in fn_nodes:
        if function.decorator_list or function.returns or function.args.defaults or function.args.kw_defaults:
            raise ValueError("Decorators, annotations and default arguments are not supported")
        if tuple(arg.arg for arg in function.args.args) != expected_signatures.get(function.name):
            raise ValueError(f"Function signature mismatch for {function.name} in {filename}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ValueError("Private names are forbidden")
        if isinstance(node, ast.Attribute) and node.attr not in allowed_methods:
            raise ValueError("Attribute access is not allowed")
        if isinstance(node, ast.Call):
            if not (
                (isinstance(node.func, ast.Name) and node.func.id in allowed_calls)
                or (isinstance(node.func, ast.Attribute) and node.func.attr in allowed_methods)
            ):
                raise ValueError("Call target is not allowed")
        if isinstance(node, (ast.Lambda, ast.ClassDef, ast.Global, ast.Nonlocal, ast.AsyncFunctionDef)):
            raise ValueError("Unsupported code construct")
        if isinstance(node, (ast.Import, ast.While, ast.For, ast.Try, ast.With)):
            raise ValueError(f"Forbidden AST construct in controlled patch: {type(node).__name__}")
        if isinstance(node, ast.ImportFrom) and node not in tree.body:
            raise ValueError("Imports are only allowed at module top-level")

    fn_names = [n.name for n in fn_nodes]
    if set(fn_names) != set(expected_signatures.keys()):
        raise ValueError(f"Expected functions {list(expected_signatures.keys())} in {filename}, got {fn_names}")
    return source


def validate_multifile_patch(raw: Any, project: str = "order_service") -> Dict[str, str]:
    """
    Validate a multi-file patch dictionary for `order_service`.
    Guardrail #2: Strictly rejects any path touching `tests/` or outside `editable_files`.
    """
    key = canonical_project(project)
    spec = PROJECTS[key]
    if not spec.get("multi_file"):
        raise ValueError(f"Project '{key}' is not a multi-file project")

    parsed: Any = raw
    if isinstance(raw, str):
        parsed = json.loads(raw)
    if isinstance(parsed, dict) and "files" in parsed and isinstance(parsed["files"], dict):
        files_input = parsed["files"]
    elif isinstance(parsed, dict) and "source" in parsed:
        src_val = parsed["source"]
        files_input = json.loads(src_val) if isinstance(src_val, str) else src_val
    elif isinstance(parsed, dict):
        files_input = parsed
    else:
        raise ValueError("Multi-file patch must provide a dictionary mapping filenames to Python source")

    if not isinstance(files_input, dict) or not files_input:
        raise ValueError("Multi-file patch dictionary cannot be empty")

    editable = set(spec["editable_files"])
    for rel_path in files_input:
        norm = str(rel_path).replace("\\", "/").lstrip("/")
        if norm.startswith("tests/") or "test_" in norm or norm not in editable:
            raise ValueError(f"Guardrail violation: patch is forbidden from modifying or deleting '{rel_path}'")

    merged_files = dict(spec["files"])
    for rel_path, content in files_input.items():
        norm = str(rel_path).replace("\\", "/").lstrip("/")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"File '{norm}' cannot be empty or deleted")
        merged_files[norm] = content

    validated_files: Dict[str, str] = {}
    for fname in spec["editable_files"]:
        validated_files[fname] = _validate_single_module_ast(
            merged_files[fname],
            filename=fname,
            expected_signatures=spec["signatures"][fname],
            allowed_imports=spec["allowed_imports"].get(fname, set()),
        )
    return validated_files


def validate_patch(raw: Any, project: str = "pricing") -> str:
    key = canonical_project(project)
    spec = PROJECTS[key]
    if spec.get("multi_file"):
        validated_map = validate_multifile_patch(raw, project=key)
        return json.dumps(validated_map, ensure_ascii=False, indent=2)
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

    return _validate_single_module_ast(
        source,
        filename=spec["file"],
        expected_signatures=spec["signatures"],
        allowed_imports=set(),
    )


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


def _run_unittest_file_in_subprocess(workspace_dir: Path, test_rel_path: str, timeout: float = 5.0) -> Dict[str, Any]:
    """
    Guardrail #3 & #4: Run `unittest` against real on-disk `.py` files inside an isolated child process.
    Strictly requires:
    - `tests_run > 0`
    - `skipped < tests_run`
    - `failures == 0`
    - `errors == 0`
    - `timed_out == False`
    """
    runner_code = (
        "import json, sys, unittest\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "loader = unittest.TestLoader()\n"
        "suite = loader.discover(start_dir=sys.argv[1], pattern=sys.argv[2])\n"
        "stream = unittest.runner._WritelnDecorator(sys.stderr)\n"
        "runner = unittest.TextTestRunner(stream=stream, verbosity=2)\n"
        "res = runner.run(suite)\n"
        "payload = {\n"
        "    'tests_run': res.testsRun,\n"
        "    'skipped': len(res.skipped),\n"
        "    'failures': len(res.failures),\n"
        "    'errors': len(res.errors),\n"
        "    'failure_details': [f'{t}: {msg}' for t, msg in (res.failures + res.errors)],\n"
        "}\n"
        "print('__UNITTEST_JSON__:' + json.dumps(payload))\n"
    )
    test_file = Path(test_rel_path)
    start_dir = str((workspace_dir / test_file.parent).resolve())
    pattern = test_file.name
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(workspace_dir.resolve())}
    if os.name == "nt" and os.environ.get("SystemRoot"):
        env["SystemRoot"] = os.environ["SystemRoot"]

    timed_out = False
    try:
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", runner_code, start_dir, pattern],
            cwd=str(workspace_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
        stderr = (exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
        exit_code = -1

    parsed_metrics = {"tests_run": 0, "skipped": 0, "failures": 0, "errors": 1 if timed_out else 0, "failure_details": []}
    for line in stdout.splitlines():
        if line.startswith("__UNITTEST_JSON__:"):
            try:
                parsed_metrics = json.loads(line.split("__UNITTEST_JSON__:", 1)[1])
            except Exception:
                pass

    tests_run = int(parsed_metrics.get("tests_run", 0))
    skipped = int(parsed_metrics.get("skipped", 0))
    failures = int(parsed_metrics.get("failures", 0))
    errors = int(parsed_metrics.get("errors", 0))

    # Guardrail #3: Zero tests, all skipped, any failure/error, or timeout is strictly NOT success
    strict_pass = (
        not timed_out
        and exit_code == 0
        and tests_run > 0
        and skipped < tests_run
        and failures == 0
        and errors == 0
    )
    passed = max(0, tests_run - skipped - failures - errors) if not timed_out else 0
    return {
        "total": tests_run,
        "tests_run": tests_run,
        "passed": passed,
        "skipped": skipped,
        "failed": failures,
        "failures": failures,
        "errors": errors,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "success": strict_pass,
        "log": (stderr + "\n" + stdout).strip()[:4000],
        "details": parsed_metrics.get("failure_details", []),
    }


def _snapshot_tests_dir_sha256(tests_dir: Path) -> Dict[str, str]:
    digests: Dict[str, str] = {}
    if not tests_dir.is_dir():
        return digests
    for p in sorted(tests_dir.rglob("*.py")):
        rel = p.relative_to(tests_dir.parent).as_posix()
        digests[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return digests


def run_multifile_check(
    raw_patch: Any,
    project: str = "order_service",
    workspace_dir: Optional[Path] = None,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """
    Execute multi-file patch verification on disk inside an isolated workspace directory.
    Enforces Guardrails #1, #2, #3, #4, #5:
    - Validates patch AST & path whitelist (forbids touching `tests/`)
    - Writes real `.py` files to disk in `workspace_dir`
    - Verifies `tests/` directory SHA-256 before and after running `unittest`
    - Runs `tests/test_public.py` and `tests/test_hidden.py` in isolated subprocesses
    - Computes canonical `source_sha256` and per-file SHA-256 map from the exact on-disk files that passed tests
    """
    key = canonical_project(project)
    spec = PROJECTS[key]
    validated_files = validate_multifile_patch(raw_patch, project=key)

    own_temp = None
    if workspace_dir is None:
        own_temp = tempfile.TemporaryDirectory(prefix="aios_order_ws_")
        ws_root = Path(own_temp.name) / key
    else:
        ws_root = Path(workspace_dir) / key

    try:
        ws_root.mkdir(parents=True, exist_ok=True)
        tests_dir = ws_root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        # Write immutable test suite to disk and record pre-run SHA-256
        expected_test_hashes: Dict[str, str] = {}
        for rel_test_path, test_content in spec["tests"].items():
            target_test = ws_root / rel_test_path
            target_test.parent.mkdir(parents=True, exist_ok=True)
            raw_bytes = test_content.encode("utf-8")
            target_test.write_bytes(raw_bytes)
            expected_test_hashes[rel_test_path] = hashlib.sha256(raw_bytes).hexdigest()

        pre_test_hashes = _snapshot_tests_dir_sha256(tests_dir)
        if pre_test_hashes != expected_test_hashes:
            raise RuntimeError("Guardrail #2 violation: pre-run test file SHA-256 mismatch")

        # Guardrail #4: Write patched `.py` files to actual disk workspace before running tests
        for rel_path, content in validated_files.items():
            target_file = ws_root / rel_path
            target_file.write_bytes(content.encode("utf-8"))

        pub_res = _run_unittest_file_in_subprocess(ws_root, "tests/test_public.py", timeout=timeout)
        held_res = _run_unittest_file_in_subprocess(ws_root, "tests/test_hidden.py", timeout=timeout)

        # Guardrail #2: Re-verify test files SHA-256 after test execution
        post_test_hashes = _snapshot_tests_dir_sha256(tests_dir)
        tests_intact = (post_test_hashes == expected_test_hashes)
        if not tests_intact:
            raise RuntimeError("Guardrail #2 violation: test files were modified or deleted during execution")

        # Guardrail #5: Read back on-disk source files that actually ran and compute canonical tree SHA-256
        disk_files: Dict[str, str] = {}
        file_hashes: Dict[str, str] = {}
        unified_diffs: List[str] = []
        for fname in spec["editable_files"]:
            on_disk_text = (ws_root / fname).read_text(encoding="utf-8")
            disk_files[fname] = on_disk_text
            file_hashes[fname] = hashlib.sha256(on_disk_text.encode("utf-8")).hexdigest()
            orig_lines = spec["files"][fname].splitlines(keepends=True)
            new_lines = on_disk_text.splitlines(keepends=True)
            diff_text = "".join(
                difflib.unified_diff(orig_lines, new_lines, fromfile=f"a/{fname}", tofile=f"b/{fname}")
            )
            if diff_text:
                unified_diffs.append(diff_text)

        tree_sha256 = compute_tree_sha256(disk_files)
        canonical_source_json = json.dumps(disk_files, ensure_ascii=False, indent=2)
        overall_success = bool(pub_res["success"] and held_res["success"] and tests_intact)

        # Guardrail #1: Redact hidden test details from feedback so hidden test assertions never leak to round 2 prompt
        safe_heldout_summary = {
            "total": held_res["total"],
            "tests_run": held_res["tests_run"],
            "passed": held_res["passed"],
            "skipped": held_res["skipped"],
            "failed": held_res["failed"],
            "failures": held_res["failures"],
            "errors": held_res["errors"],
            "timed_out": held_res["timed_out"],
            "exit_code": held_res["exit_code"],
            "success": held_res["success"],
        }

        return {
            "project": key,
            "multi_file": True,
            "success": overall_success,
            "files": disk_files,
            "file_hashes": file_hashes,
            "test_hashes": post_test_hashes,
            "tests_intact": tests_intact,
            "unified_diff": "\n".join(unified_diffs),
            "source": canonical_source_json,
            "source_sha256": tree_sha256,
            "tree_sha256": tree_sha256,
            "workspace_written": True,
            "public": pub_res,
            "heldout": safe_heldout_summary,
            "failing_count": pub_res["failed"] + pub_res["errors"] + held_res["failed"] + held_res["errors"],
        }
    finally:
        if own_temp is not None:
            own_temp.cleanup()


def build_delivery_zip_bytes(checked_result: Dict[str, Any], workflow_id: str, *, evidence=None) -> Tuple[bytes, Dict[str, Any]]:
    """
    Guardrail #5: Package the verified source tree, unified diff patch, test execution logs,
    and `SHA256SUMS.txt` into a deterministic downloadable ZIP archive, and verify that the
    source tree inside the archive matches `checked_result['source_sha256']`.
    """
    project = checked_result.get("project", "order_service")
    spec = PROJECTS[canonical_project(project)]
    if spec.get("multi_file"):
        files_map = dict(checked_result["files"])
        expected_tree_sha = checked_result["source_sha256"]
        if compute_tree_sha256(files_map) != expected_tree_sha:
            raise RuntimeError("Guardrail #5 violation: candidate files hash does not match verified tree_sha256")
    else:
        fname = spec["file"]
        src = checked_result["source"]
        if hashlib.sha256(src.encode("utf-8")).hexdigest() != checked_result["source_sha256"]:
            raise RuntimeError("Guardrail #5 violation: candidate source hash mismatch")
        files_map = {fname: src}
        expected_tree_sha = checked_result["source_sha256"]

    diff_content = checked_result.get("unified_diff") or ""
    if not diff_content and not spec.get("multi_file"):
        fname = spec["file"]
        diff_content = "".join(
            difflib.unified_diff(
                spec["source"].splitlines(keepends=True),
                checked_result["source"].splitlines(keepends=True),
                fromfile=f"a/{fname}",
                tofile=f"b/{fname}",
            )
        )

    test_report = {
        "workflow_id": workflow_id,
        "project": project,
        "verified_source_sha256": expected_tree_sha,
        "public": checked_result.get("public"),
        "heldout": checked_result.get("heldout"),
        "tests_intact": checked_result.get("tests_intact", True),
    }
    if spec.get("provenance"):
        test_report["provenance"] = spec["provenance"]
    test_log_text = json.dumps(test_report, ensure_ascii=False, indent=2) + "\n"

    sha_lines: List[str] = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path in sorted(files_map.keys()):
            content = files_map[rel_path]
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            arcname = f"src/{rel_path}"
            zf.writestr(arcname, content.encode("utf-8"))
            sha_lines.append(f"{digest}  {arcname}")

        if spec.get("multi_file"):
            pub_test_bytes = spec["tests"]["tests/test_public.py"].encode("utf-8")
            pub_test_sha = hashlib.sha256(pub_test_bytes).hexdigest()
            zf.writestr("tests/test_public.py", pub_test_bytes)
            sha_lines.append(f"{pub_test_sha}  tests/test_public.py")

        if spec.get("provenance"):
            supplementary = {
                "UPSTREAM.json": json.dumps(spec["provenance"], ensure_ascii=False, indent=2) + "\n",
                "LICENSE.upstream.txt": spec["license_text"],
            }
            for name, content in supplementary.items():
                raw = content.encode("utf-8")
                zf.writestr(name, raw)
                sha_lines.append(f"{hashlib.sha256(raw).hexdigest()}  {name}")

        readme = (
            "# Verified controlled project\n\n"
            "Source files are under src/. Public tests are under tests/ for the multi-file fixture.\n"
            "Hidden evaluator tests are not distributed.\n\n"
            "Linux/macOS: PYTHONPATH=src python3 -m unittest discover -s tests -v\n"
            "PowerShell: $env:PYTHONPATH='src'; python -m unittest discover -s tests -v\n"
        ).encode("utf-8") if spec.get("multi_file") else b"# Verified single-file fixture\nSee test_verification_log.json for evaluator results.\n"
        zf.writestr("README.md", readme)
        sha_lines.append(f"{hashlib.sha256(readme).hexdigest()}  README.md")

        patch_bytes = diff_content.encode("utf-8")
        patch_sha = hashlib.sha256(patch_bytes).hexdigest()
        zf.writestr("patch.diff", patch_bytes)
        sha_lines.append(f"{patch_sha}  patch.diff")

        log_bytes = test_log_text.encode("utf-8")
        log_sha = hashlib.sha256(log_bytes).hexdigest()
        zf.writestr("test_verification_log.json", log_bytes)
        sha_lines.append(f"{log_sha}  test_verification_log.json")

        if evidence is not None:
            encoded = (json.dumps(evidence, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            zf.writestr("execution_evidence.json", encoded)
            sha_lines.append(f"{hashlib.sha256(encoded).hexdigest()}  execution_evidence.json")

        sha_lines.append(f"{expected_tree_sha}  __SOURCE_TREE_SHA256__")
        sums_text = "\n".join(sha_lines) + "\n"
        zf.writestr("SHA256SUMS.txt", sums_text.encode("utf-8"))

    zip_bytes = buf.getvalue()
    zip_sha256 = hashlib.sha256(zip_bytes).hexdigest()
    return zip_bytes, {
        "workflow_id": workflow_id,
        "project": project,
        "zip_sha256": zip_sha256,
        "source_tree_sha256": expected_tree_sha,
        "files_included": sorted(files_map.keys()),
        "size_bytes": len(zip_bytes),
    }


def _verify_execution_evidence(evidence, expected_source_sha256):
    try:
        if evidence["schema_version"] != 1 or evidence["verified_source_sha256"] != expected_source_sha256:
            raise ValueError("Execution evidence source hash mismatch")
        models = evidence["model"]
        selected = models.get("patch_round_2") or models["patch"]
        if selected["candidate_sha256"] != expected_source_sha256:
            raise ValueError("Delivered source differs from the evidenced candidate")
        if evidence["resumed"]:
            first, second = [worker for worker in evidence["verification_workers"]
                             if worker["operation"] == "project.patch_check"]
            verification = [node for node in evidence["nodes"] if node["task_id"] == "verify"]
            starts = [node for node in verification if node["state"] == "running"]
            completed = next(node for node in verification if node["state"] == "succeeded")
            patch_starts = [node for node in evidence["nodes"] if node["task_id"] in ("patch", "patch_round_2")
                            and node["state"] == "running"]
            if (evidence["model_calls_during_resume"] != 0 or len(patch_starts) != 1
                    or len(starts) != 2 or starts[0]["input_refs"] != starts[1]["input_refs"]
                    or first["id"] == second["id"] or not first["returncode"]
                    or first["interrupted_before_start"] is not True or first["reclaimed"] is not True
                    or second["returncode"] != 0 or second["reclaimed"] is not True
                    or first["parameters_sha256"] != second["parameters_sha256"]
                    or completed["result_sha256"] != second["result_sha256"]):
                raise ValueError("Recovery evidence does not match the resumed verification")
    except (AttributeError, KeyError, StopIteration, TypeError) as exc:
        raise ValueError("Invalid execution evidence") from exc


def verify_delivery_zip_bytes(zip_bytes: bytes, expected_source_sha256: str, multi_file: bool = True) -> Dict[str, Any]:
    """
    Guardrail #5 verifier: Extract downloaded ZIP in memory, recompute source tree SHA-256
    and every entry in `SHA256SUMS.txt`, and assert exact match with `expected_source_sha256`.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        names = set(zf.namelist())
        if "SHA256SUMS.txt" not in names or "patch.diff" not in names or "test_verification_log.json" not in names:
            raise ValueError("Delivery ZIP is missing required verification files")
        extracted_src: Dict[str, str] = {}
        for name in sorted(names):
            if name.startswith("src/") and not name.endswith("/"):
                rel = name[len("src/"):]
                extracted_src[rel] = zf.read(name).decode("utf-8")
        if not extracted_src:
            raise ValueError("Delivery ZIP contains no source files under src/")

        if multi_file:
            recomputed = compute_tree_sha256(extracted_src)
        else:
            only_file = next(iter(extracted_src.values()))
            recomputed = hashlib.sha256(only_file.encode("utf-8")).hexdigest()

        if recomputed != expected_source_sha256:
            raise ValueError(
                f"Guardrail #5 mismatch: ZIP source SHA-256 ({recomputed}) != verified workspace SHA-256 ({expected_source_sha256})"
            )
        sums_raw = zf.read("SHA256SUMS.txt").decode("utf-8")
        checksummed = set()
        for line in sums_raw.strip().splitlines():
            digest, arcname = line.split("  ", 1)
            checksummed.add(arcname)
            if arcname == "__SOURCE_TREE_SHA256__":
                if digest != expected_source_sha256:
                    raise ValueError("SHA256SUMS.txt tree hash does not match expected_source_sha256")
            else:
                actual_file_sha = hashlib.sha256(zf.read(arcname)).hexdigest()
                if actual_file_sha != digest:
                    raise ValueError(f"SHA256SUMS.txt entry mismatch for {arcname}")
        result = {
            "verified": True,
            "source_tree_sha256": recomputed,
            "files": extracted_src,
            "sha256sums": sums_raw,
        }
        if "execution_evidence.json" in names:
            if "execution_evidence.json" not in checksummed:
                raise ValueError("Execution evidence is missing its checksum")
            evidence = json.loads(zf.read("execution_evidence.json"))
            _verify_execution_evidence(evidence, recomputed)
            result["execution_evidence"] = evidence
        return result


def run_check(source: Any, project: str = "pricing", workspace_dir: Optional[Path] = None) -> Dict[str, Any]:
    key = canonical_project(project)
    spec = PROJECTS[key]
    if spec.get("multi_file"):
        return run_multifile_check(source, project=key, workspace_dir=workspace_dir)
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
