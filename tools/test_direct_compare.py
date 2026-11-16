"""Offline grader checks; performs no paid model requests."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('comparison',Path(__file__).with_name('compare_direct_runtime.py'))
benchmark=importlib.util.module_from_spec(spec);spec.loader.exec_module(benchmark)


class ComparisonTests(unittest.TestCase):
    def test_exact_fields_do_not_accept_boolean_integer_substitution(self):
        case={'group':'plain','expected':{'amount':1,'valid':True}}
        self.assertFalse(benchmark.evaluate(case,{'status':'succeeded','answer':'{"amount":true,"valid":1}'},None,None)['task_passed'])
        self.assertTrue(benchmark.evaluate(case,{'status':'succeeded','answer':'{"amount":1,"valid":true}'},None,None)['task_passed'])
        self.assertFalse(benchmark.evaluate(case,{'status':'failed','answer':'{"amount":1,"valid":true}'},None,None)['task_passed'])

    def test_format_and_content_scored_separately(self):
        parsed,strict=benchmark.object_from('```json\n{"answer":2}\n```')
        self.assertEqual(parsed,{'answer':2});self.assertFalse(strict)
        self.assertEqual(benchmark.object_from('no JSON'),(None,False))

    def test_correct_sources_pass_fresh_unseen_cases_and_wrong_patch_fails(self):
        sources={
        'pricing':'''def line_total_cents(unit_cents,quantity,discount_pct):
    if unit_cents<0 or quantity<0 or discount_pct<0 or discount_pct>100:
        raise ValueError("invalid")
    return unit_cents*quantity*(100-discount_pct)//100
def shipping_cents(subtotal_cents):
    if subtotal_cents<0:
        raise ValueError("invalid")
    if subtotal_cents>=5000:
        return 0
    return 599
def refund_cents(paid_cents,returned_qty,total_qty):
    if paid_cents<0 or total_qty<=0 or returned_qty<0 or returned_qty>total_qty:
        raise ValueError("invalid")
    return paid_cents*returned_qty//total_qty
''',
        'intervals':'''def overlap_seconds(start_a,end_a,start_b,end_b):
    if end_a<start_a or end_b<start_b:
        raise ValueError("invalid")
    return max(0,min(end_a,end_b)-max(start_a,start_b))
def bucket_index(timestamp,origin,width):
    if width<=0:
        raise ValueError("invalid")
    return (timestamp-origin)//width
def remaining_ttl(now,created,ttl):
    if ttl<0:
        raise ValueError("invalid")
    return max(0,ttl-max(0,now-created))
''',
        'text_normalization':'''def normalize_label(text):
    return ' '.join(text.split()).lower()
def word_count(text):
    return len(text.split())
def starts_with(text,prefix):
    return text.strip().lower().startswith(prefix.strip().lower())
'''}
        from agent_runtime.engineering import PROJECTS
        with tempfile.TemporaryDirectory() as tmp:
            for project,source in sources.items():
                case={'group':'code','project':project}
                result=benchmark.evaluate(case,{'status':'succeeded','answer':json.dumps({'source':source})},tmp,project)
                self.assertTrue(result['task_passed'],result);self.assertFalse(result['runtime_verified_same_patch'])
                self.assertGreater(result['unseen']['total'],70)
                wrong=benchmark.evaluate(case,{'status':'succeeded','answer':json.dumps({'source':PROJECTS[project]['source']})},tmp,project+'-wrong')
                self.assertFalse(wrong['task_passed'])


if __name__=='__main__':unittest.main()
