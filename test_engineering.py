import json
from pathlib import Path
import tempfile
import unittest
from agent_runtime.engineering import BASELINE_SOURCE,project_context,manifest,validate_patch,run_check


# Correct implementation is test-only and is never included in model context.
REFERENCE = '''def line_total_cents(unit_cents, quantity, discount_pct):
    if unit_cents < 0 or quantity < 0 or discount_pct < 0 or discount_pct > 100:
        raise ValueError("invalid")
    return unit_cents * quantity * (100 - discount_pct) // 100

def shipping_cents(subtotal_cents):
    if subtotal_cents < 0:
        raise ValueError("invalid")
    if subtotal_cents >= 5000:
        return 0
    return 599

def refund_cents(paid_cents, returned_qty, total_qty):
    if paid_cents < 0 or total_qty <= 0 or returned_qty < 0 or returned_qty > total_qty:
        raise ValueError("invalid")
    return paid_cents * returned_qty // total_qty
'''


class EngineeringTests(unittest.TestCase):
    def test_model_context_is_short_and_does_not_include_reference_or_heldout(self):
        context=project_context()
        self.assertLessEqual(len(context),2300)
        self.assertEqual(json.loads(context),manifest())
        self.assertNotIn('HELDOUT',context)
        self.assertNotIn('unit_cents * quantity * (100 - discount_pct) // 100',context)

    def test_baseline_red_reference_green_with_heldout_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            report=run_check(validate_patch(json.dumps({'source':REFERENCE})),directory)
            self.assertGreater(report['baseline_failing_count'],0)
            self.assertTrue(report['success'])
            self.assertEqual(report['public']['passed'],report['public']['total'])
            self.assertEqual(report['heldout']['passed'],report['heldout']['total'])
            self.assertTrue(Path(report['report_path']).is_file())
            self.assertEqual(Path(report['workspace'],'candidate/pricing.py').read_text(),REFERENCE)

    def test_no_false_success_for_original_or_partial_fix(self):
        with tempfile.TemporaryDirectory() as directory:
            original=run_check(BASELINE_SOURCE,directory)
            self.assertFalse(original['success'])
            self.assertGreater(original['heldout']['failed'],0)
            partial=run_check(BASELINE_SOURCE.replace('subtotal_cents > 5000','subtotal_cents >= 5000'),directory)
            self.assertFalse(partial['success'])
            self.assertGreater(partial['public']['failed'],0)

    def test_patch_cannot_edit_paths_tests_or_use_duplicate_json_fields(self):
        for raw in ({'source':REFERENCE,'path':'../../test.py'},
                    {'files':{'pricing.py':REFERENCE}},
                    '{"source":"a","source":"b"}',
                    '```json\n'+json.dumps({'source':REFERENCE})+'\n```'):
            with self.assertRaises(ValueError):validate_patch(raw)

    def test_escape_capabilities_and_unbounded_execution_are_rejected(self):
        attacks=[
            'import os\n'+REFERENCE,
            REFERENCE.replace('return 599','return __import__("os").system("id")'),
            REFERENCE.replace('return 599','return (1).__class__'),
            REFERENCE.replace('return 599','return open("a","w")'),
            REFERENCE.replace('return 599','return [1][0]'),
            REFERENCE.replace('return 599','while 1:\n        return 599'),
            REFERENCE.replace('return 599','return 2 ** 999999999'),
            REFERENCE.replace('return 599','return shipping_cents(1)'),
            REFERENCE.replace('def shipping_cents(subtotal_cents):','@max\ndef shipping_cents(subtotal_cents):'),
            REFERENCE.replace('def shipping_cents(subtotal_cents):','def shipping_cents(subtotal_cents: int):'),
        ]
        for source in attacks:
            with self.subTest(source=source[-100:]),self.assertRaises(ValueError):
                validate_patch({'source':source})

    def test_builtin_aliases_overrides_and_signature_tricks_are_rejected(self):
        attacks=[
            REFERENCE.replace('return 599','min = 1\n    return 599'),
            REFERENCE.replace('return 599','ValueError = 1\n    return 599'),
            REFERENCE.replace('return 599','alias = min\n    return alias(1, 2)'),
            REFERENCE.replace('return 599','return globals()'),
            REFERENCE.replace('def shipping_cents(subtotal_cents):','def shipping_cents(subtotal_cents=1):'),
            REFERENCE.replace('def shipping_cents(subtotal_cents):','def shipping_cents(subtotal_cents, /):'),
            REFERENCE.replace('def shipping_cents(subtotal_cents):','def shipping_cents(*subtotal_cents):'),
            REFERENCE.replace('def shipping_cents(subtotal_cents):','def shipping_cents(subtotal_cents) -> int:'),
            REFERENCE.replace('return 599','return int(599 / 1)'),
            REFERENCE.replace('return 599','return round(599 / 1)'),
        ]
        for source in attacks:
            with self.subTest(source=source[-100:]),self.assertRaises(ValueError):
                validate_patch({'source':source})

    def test_workspace_existing_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            unrelated=root/'pricing.py';unrelated.write_text('preserve me')
            report=run_check(REFERENCE,root)
            self.assertEqual(unrelated.read_text(),'preserve me')
            self.assertEqual(Path(report['workspace']).parent,root)
            self.assertEqual(json.loads(Path(report['workspace'],'immutable_cases.json').read_text())['public'][0][0],
                             'line_total_cents')


if __name__=='__main__':unittest.main()
