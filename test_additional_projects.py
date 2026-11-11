import json
from pathlib import Path
import tempfile
import unittest
from agent_runtime.engineering import (project_ids,project_metadata,manifest,project_context,
                                       validate_patch,run_check)


# Reference implementations stay in tests, never in model-facing manifests.
INTERVAL_REFERENCE='''def overlap_seconds(start_a, end_a, start_b, end_b):
    if end_a < start_a or end_b < start_b:
        raise ValueError("invalid")
    return max(0, min(end_a, end_b) - max(start_a, start_b))
def bucket_index(timestamp, origin, width):
    if width <= 0:
        raise ValueError("invalid")
    return (timestamp - origin) // width
def remaining_ttl(now, created, ttl):
    if ttl < 0:
        raise ValueError("invalid")
    return max(0, min(ttl, ttl - (now - created)))
'''
TEXT_REFERENCE='''def normalize_label(text):
    cleaned = " ".join(text.split())
    return cleaned.lower()
def word_count(text):
    return len(text.split())
def starts_with(text, prefix):
    return text.strip().lower().startswith(prefix.strip().lower())
'''


class AdditionalProjectTests(unittest.TestCase):
    def test_metadata_and_contexts_are_bounded_and_hide_reference(self):
        self.assertEqual(project_ids(),('pricing','intervals','text_normalization'))
        self.assertEqual(len(project_metadata()),3)
        for key in project_ids():
            with self.subTest(project=key):
                context=project_context(key)
                self.assertLessEqual(len(context),3000)
                self.assertEqual(json.loads(context),manifest(key))
                self.assertGreaterEqual(project_metadata(key)['public_tests'],6)
                self.assertGreaterEqual(project_metadata(key)['heldout_tests'],10)
                self.assertNotIn('HELDOUT',context)
        self.assertNotIn('max(0, min(end_a',project_context('intervals'))
        self.assertNotIn('cleaned =',project_context('text_normalization'))

    def test_interval_original_red_reference_green(self):
        with tempfile.TemporaryDirectory() as directory:
            result=run_check(INTERVAL_REFERENCE,directory,'intervals')
            self.assertTrue(result['success'],result['stdout'])
            self.assertGreater(result['baseline_failing_count'],0)
            self.assertEqual(result['heldout']['passed'],result['heldout']['total'])
            self.assertTrue(Path(result['workspace'],'candidate/intervals.py').is_file())

    def test_text_original_red_reference_green_and_types(self):
        with tempfile.TemporaryDirectory() as directory:
            result=run_check(TEXT_REFERENCE,directory,'text_normalization')
            self.assertTrue(result['success'],result['stdout'])
            self.assertGreater(result['baseline_failing_count'],0)
            self.assertEqual(result['public']['passed'],result['public']['total'])
            wrong=TEXT_REFERENCE.replace('return text.strip().lower().startswith(prefix.strip().lower())','return 1')
            wrong_result=run_check(wrong,directory,'text_normalization')
            self.assertFalse(wrong_result['success'])

    def test_string_capability_escapes_are_rejected(self):
        attacks=[
            TEXT_REFERENCE.replace('return len(text.split())','return text.__class__'),
            TEXT_REFERENCE.replace('return len(text.split())','return text.format()'),
            TEXT_REFERENCE.replace('return len(text.split())','return text.encode()'),
            TEXT_REFERENCE.replace('return len(text.split())','return open(text)'),
            TEXT_REFERENCE.replace('return len(text.split())','return text.split()[0]'),
            TEXT_REFERENCE.replace('return len(text.split())','f = text.strip\n    return f()'),
            TEXT_REFERENCE.replace('return len(text.split())','len = min\n    return len(text)'),
            TEXT_REFERENCE.replace('return len(text.split())','return len.__call__(text)'),
            TEXT_REFERENCE.replace('return len(text.split())','return text.startswith.__globals__'),
            TEXT_REFERENCE.replace('return len(text.split())','return "{x}".format_map(text)'),
            TEXT_REFERENCE.replace('return len(text.split())','return normalize_label(text)'),
            TEXT_REFERENCE.replace('return len(text.split())','return text.strip(chars="a")'),
        ]
        for source in attacks:
            with self.subTest(tail=source[-100:]),self.assertRaises(ValueError):
                validate_patch({'source':source},'text_normalization')

    def test_pricing_does_not_gain_string_capabilities(self):
        source=manifest()['source'].replace('return 599','return "abc".lower()')
        with self.assertRaises(ValueError):validate_patch({'source':source})

    def test_unknown_projects_and_paths_are_rejected(self):
        for key in ('../pricing','text','',None):
            with self.assertRaises(ValueError):manifest(key)
        with self.assertRaises(ValueError):
            validate_patch({'source':TEXT_REFERENCE,'path':'../../tests.py'},'text_normalization')


if __name__=='__main__':unittest.main()
