"""Offline acceptance tests: real checker processes and runtime, no provider API."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_runtime.engineering import manifest, project_ids
from agent_runtime.model_gateway import ModelGateway
from agent_runtime.project_workflow import execute_project_action, project_tasks, project_view
from agent_runtime.runtime import run
from test_engineering import REFERENCE
from test_additional_projects import INTERVAL_REFERENCE, TEXT_REFERENCE


REFERENCES = {'pricing': REFERENCE, 'intervals': INTERVAL_REFERENCE,
              'text_normalization': TEXT_REFERENCE}


class OfflineRepairClient:
    """Return fixtures by stage, verifying that real dependency outputs arrive."""
    model, timeout = 'offline-project-workflow', 5

    def __init__(self, source, accepted=True, wrong_hash=False):
        self.source, self.accepted, self.wrong_hash = source, accepted, wrong_hash
        self.stages = []
        self.repair_inputs = None

    def complete(self, messages, max_tokens):
        prompt = messages[0]['content']
        upstream = json.loads(messages[-1]['content'])['upstream_results']
        if prompt.startswith('Analyze'):
            stage, content = 'analyze', 'analysis fixture: inspect the boundary defects'
            assert upstream['baseline']['expected_red'] is True
        elif prompt.startswith('Propose'):
            stage, content = 'test_plan', 'test fixture: include boundaries and invalid inputs'
            assert upstream['baseline']['baseline_failing_count'] > 0
        elif prompt.startswith('Repair'):
            stage, content = 'repair', json.dumps({'source': self.source})
            self.repair_inputs = upstream
            assert set(upstream) == {'analyze', 'test_plan'}
            assert upstream['analyze'].startswith('analysis fixture:')
            assert upstream['test_plan'].startswith('test fixture:')
        elif prompt.startswith('Review'):
            stage = 'review'
            checked = upstream['validate']
            assert checked['success'] is True
            assert checked['source'] == self.source
            content = json.dumps({'accepted': self.accepted,
                'source_sha256': 'wrong' if self.wrong_hash else checked['source_sha256'],
                'reason': 'offline fixture inspected actual checker output'})
        else:
            raise AssertionError('Unexpected model stage: ' + prompt)
        self.stages.append(stage)
        return {'content': content, 'model': self.model,
                'usage': {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18}}


class ProjectWorkflowTests(unittest.TestCase):
    def action(self, project, action, directory, inputs=None):
        return execute_project_action({'project': project, 'action': action,
            '_workspace': directory, '_inputs': inputs or {}})

    def test_all_projects_original_red_then_real_tests_green_and_hash_bound_gate(self):
        for project in project_ids():
            with self.subTest(project=project), tempfile.TemporaryDirectory() as directory:
                baseline, children = self.action(project, 'project_baseline', directory)
                self.assertTrue(baseline['expected_red'])
                self.assertGreater(baseline['baseline_failing_count'], 0)
                self.assertEqual(children, [])
                source = REFERENCES[project]
                validated, children = self.action(project, 'project_validate', directory,
                    {'repair': json.dumps({'source': source})})
                self.assertTrue(validated['success'], validated)
                self.assertEqual(validated['source_sha256'], hashlib.sha256(source.encode()).hexdigest())
                for group in ('public', 'heldout'):
                    self.assertEqual(validated[group]['passed'], validated[group]['total'])
                    self.assertEqual(validated[group]['errors'], 0)
                self.assertEqual([child['id'] for child in children], ['review', 'gate'])
                self.assertEqual(children[0]['inputs'], ['validate'])
                self.assertEqual(children[1]['inputs'], ['validate', 'review'])
                verdict, extra = self.action(project, 'project_gate', directory,
                    {'validate': validated, 'review': json.dumps({'accepted': True,
                     'source_sha256': validated['source_sha256'], 'reason': 'verified'})})
                self.assertTrue(verdict['accepted'])
                self.assertEqual(extra, [])

    def test_invalid_ast_never_executes_checker_or_spawns_paid_review(self):
        for project in project_ids():
            with self.subTest(project=project), tempfile.TemporaryDirectory() as directory:
                with patch('agent_runtime.project_workflow.run_check') as checker:
                    result, children = self.action(project, 'project_validate', directory,
                        {'repair': json.dumps({'source': 'import os\n' + REFERENCES[project]})})
                checker.assert_not_called()
                self.assertFalse(result['success'])
                self.assertIn('error', result)
                self.assertEqual(children, [])

    def test_syntactically_safe_but_failing_patch_stops_review_for_each_project(self):
        for project in project_ids():
            with self.subTest(project=project), tempfile.TemporaryDirectory() as directory:
                result, children = self.action(project, 'project_validate', directory,
                    {'repair': json.dumps({'source': manifest(project)['source']})})
                self.assertFalse(result['success'])
                self.assertGreater(result['public']['failed'] + result['public']['errors'], 0)
                self.assertEqual(children, [])
                view = project_view(project, [{'task_id': 'validate', 'state': 'succeeded',
                    'result': {'result': result}}], {'validate': 'succeeded'})
                phases = {p['id']: p['status'] for p in view['phases']}
                self.assertEqual(phases['validate'], 'failed')
                self.assertEqual(phases['review'], 'skipped')
                self.assertEqual(phases['gate'], 'skipped')

    def test_gate_fails_closed_for_rejection_wrong_hash_and_invalid_protocol(self):
        validation = {'success': True, 'source_sha256': 'correct'}
        reviews = [json.dumps({'accepted': False, 'source_sha256': 'correct'}),
                   json.dumps({'accepted': True, 'source_sha256': 'wrong'}),
                   json.dumps({'accepted': 1, 'source_sha256': 'correct'}),
                   json.dumps({'accepted': 'true', 'source_sha256': 'correct'}),
                   json.dumps({'accepted': True, 'source_sha256': 'correct', 'reason': []}),
                   'not JSON', '[]', 'null']
        for review in reviews:
            with self.subTest(review=review):
                verdict, children = self.action('pricing', 'project_gate', '.',
                    {'validate': validation, 'review': review})
                self.assertFalse(verdict['accepted'])
                self.assertEqual(children, [])
        verdict, _ = self.action('pricing', 'project_gate', '.',
            {'validate': {**validation, 'success': False}, 'review':
             json.dumps({'accepted': True, 'source_sha256': 'correct'})})
        self.assertFalse(verdict['accepted'])

    def run_offline(self, project, source, **client_options):
        with tempfile.TemporaryDirectory() as directory:
            client = OfflineRepairClient(source, **client_options)
            gateway = ModelGateway(client, Path(directory) / 'offline-calls.jsonl')
            with patch('agent_runtime.runtime.ModelGateway.from_env', return_value=gateway):
                states, events = run(project_tasks(project), output_dir=directory, quiet=True)
            view = project_view(project, events, states)
            return client, states, events, view

    def test_real_worker_runtime_green_for_three_distinct_projects(self):
        for project in project_ids():
            with self.subTest(project=project):
                client, states, events, view = self.run_offline(project, REFERENCES[project])
                self.assertEqual(len(states), 7, events)
                self.assertTrue(all(state == 'succeeded' for state in states.values()), events)
                self.assertEqual(set(client.stages), {'analyze', 'test_plan', 'repair', 'review'})
                self.assertEqual(len(client.stages), 4)
                self.assertEqual(view['model_calls'], 4)
                self.assertEqual(view['usage']['total_tokens'], 72)
                self.assertTrue(view['baseline']['expected_red'])
                self.assertTrue(view['validation']['success'])
                self.assertTrue(view['verdict']['accepted'])
                self.assertEqual(view['patch_source'], REFERENCES[project])

    def test_real_worker_runtime_invalid_or_red_patch_never_calls_review(self):
        for source in ('import os\n' + REFERENCE, manifest()['source']):
            with self.subTest(source=source[:30]):
                client, states, events, view = self.run_offline('pricing', source)
                self.assertEqual(len(client.stages), 3, events)
                self.assertNotIn('review', client.stages)
                self.assertNotIn('review', states)
                self.assertNotIn('gate', states)
                self.assertFalse(view['validation']['success'])
                self.assertIsNone(view['verdict'])
                phases = {p['id']: p['status'] for p in view['phases']}
                self.assertEqual(phases['review'], 'skipped')
                self.assertEqual(phases['gate'], 'skipped')

    def test_real_worker_runtime_reviewer_rejection_is_visible_as_failed_gate(self):
        for options in ({'accepted': False}, {'wrong_hash': True}):
            with self.subTest(options=options):
                client, states, events, view = self.run_offline('pricing', REFERENCE, **options)
                self.assertEqual(len(client.stages), 4, events)
                self.assertTrue(view['validation']['success'])
                self.assertFalse(view['verdict']['accepted'])
                self.assertEqual(next(p['status'] for p in view['phases'] if p['id'] == 'gate'), 'failed')


if __name__ == '__main__':
    unittest.main()
