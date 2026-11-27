import io
import json
import unittest
import zipfile

from aios.tool.engineering import build_delivery_zip_bytes, manifest, run_check, verify_delivery_zip_bytes
from runtime.engineering_protocol import analysis_plan, patch_messages


class UpstreamEngineeringTest(unittest.TestCase):
    def test_baseline_and_repair_deliver_vetted_source_with_attribution(self):
        project = 'json_pointer'
        spec = manifest(project)
        baseline = run_check(spec['source'], project)
        self.assertFalse(baseline['success'])
        self.assertEqual(baseline['public']['failures'], 2)
        repaired = run_check(spec['reference_fix'], project)
        self.assertTrue(repaired['success'])
        self.assertEqual(repaired['public']['passed'], 3)
        self.assertEqual(repaired['heldout']['passed'], 3)
        self.assertEqual(repaired['test_hashes'], baseline['test_hashes'])

        raw, _ = build_delivery_zip_bytes(repaired, 'upstream-test')
        verified = verify_delivery_zip_bytes(raw, repaired['source_sha256'])
        self.assertEqual(verified['files'], repaired['files'])
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            self.assertEqual(json.loads(bundle.read('UPSTREAM.json')), spec['provenance'])
            self.assertIn('Stefan Kögl', bundle.read('LICENSE.upstream.txt').decode())
            self.assertNotIn('tests/test_hidden.py', bundle.namelist())

    def test_prompt_uses_project_files_and_excludes_hidden_verification_details(self):
        project = 'json_pointer'
        spec = manifest(project, include_reference=False)
        baseline = run_check(spec['source'], project)
        plan = analysis_plan(project, spec, baseline)
        for data, retry in ((plan, False), (baseline, True)):
            messages = patch_messages(project, spec, data, retry=retry)
            encoded = json.dumps(messages, ensure_ascii=False)
            self.assertIn('pointer_codec.py', encoded)
            self.assertIn('pointer_path.py', encoded)
            self.assertNotIn('pricing.py', encoded)
            self.assertNotIn('test_hidden', encoded)
            self.assertNotIn('reference_fix', encoded)
