import json
from pathlib import Path
import tempfile
import unittest
from tools.analyze_real_experiments import summarize,write_report


def sample(**extra):
    return {'id':'live-1','kind':'live','scenario':'pricing','workers':2,'policy':'resource_fit',
            'cache_mode':'lru2','model':'fixture','success':True,'wall_seconds':2,
            'usage':{'prompt_tokens':1000,'completion_tokens':100,'prompt_cache_hit_tokens':400,
                     'prompt_cache_miss_tokens':600},'model_calls':4,'billed_model_calls':4,
            **extra}


class AnalysisTests(unittest.TestCase):
    def test_live_and_replay_not_merged_or_rebilled(self):
        report=summarize([sample(),sample(id='replay-1',kind='replay',billed_model_calls=0)],
                         {'currency':'CNY','input_per_million':2,'output_per_million':8,'cached_input_per_million':.2})
        self.assertEqual(len(report['groups']),2)
        self.assertAlmostEqual(report['records'][0]['estimated_api_cost'],.00208)
        self.assertEqual(report['records'][1]['estimated_api_cost'],0)
        self.assertIsNone(report['records'][0]['actual_api_cost'])
        with self.assertRaises(ValueError):summarize([sample(kind='replay')])

    def test_missing_observations_stay_unknown_and_small_n_is_flagged(self):
        report=summarize([sample(usage={})])
        group=report['groups'][0]
        self.assertTrue(group['small_sample_warning'])
        self.assertIsNone(group['metrics']['cpu_seconds']['median'])
        self.assertEqual(group['metrics']['cpu_seconds']['observations'],0)
        self.assertIsNone(group['estimated_api_cost'])

    def test_completion_and_ready_wait_aggregation(self):
        report=summarize([sample(ready_wait_seconds={'a':1,'b':3}),
                          sample(id='live-2',success=False,wall_seconds=4,ready_wait_seconds={'a':2,'b':6})])
        group=report['groups'][0]
        self.assertEqual(group['completion_rate'],.5)
        self.assertEqual(group['metrics']['wall_seconds']['median'],3)
        self.assertEqual(group['metrics']['ready_wait_seconds']['median'],3)
        self.assertEqual(group['metrics']['max_ready_wait_seconds']['median'],4.5)

    def test_reports_do_not_copy_extra_sensitive_content(self):
        report=summarize([sample(prompt='PRIVATE-PROMPT',api_key='PRIVATE-KEY',
            test_results={'public':{'passed':3,'stdout':'PRIVATE-OUTPUT'}})])
        with tempfile.TemporaryDirectory() as directory:
            write_report(report,directory)
            text=''.join(path.read_text(encoding='utf-8-sig') for path in Path(directory).iterdir())
            for secret in ('PRIVATE-PROMPT','PRIVATE-KEY','PRIVATE-OUTPUT'):self.assertNotIn(secret,text)
            self.assertIn('n=1',text)

    def test_inconsistent_cache_counts_do_not_produce_fictional_price(self):
        row=sample(usage={'prompt_tokens':100,'completion_tokens':5,'prompt_cache_hit_tokens':200})
        with self.assertRaises(ValueError):
            summarize([row],{'currency':'CNY','input_per_million':2,'output_per_million':8,'cached_input_per_million':.2})

    def test_credential_shaped_identifier_is_rejected_without_echo(self):
        secret='sk-'+'x'*24
        with self.assertRaises(ValueError) as captured:
            summarize([sample(id=secret)])
        self.assertNotIn(secret,str(captured.exception))

    def test_empty_content_diagnostic_failures_still_count_toward_cost(self):
        rates={'currency':'CNY','input_per_million':2,'output_per_million':8,'cached_input_per_million':.04}
        failed=sample(id='empty-content-1',scenario='empty-content-diagnostic',success=False,
                      usage={'prompt_tokens':1000,'completion_tokens':512,
                             'prompt_cache_hit_tokens':400,'prompt_cache_miss_tokens':600,
                             'reasoning_tokens':512})
        good=sample(id='fixed-1')
        report=summarize([failed,good],rates)
        failed_cost=report['records'][0]['estimated_api_cost']
        self.assertAlmostEqual(failed_cost,.005312)
        self.assertGreater(failed_cost,0)
        total=report['totals_by_kind']['live']
        self.assertEqual(total['failed_records'],1)
        self.assertAlmostEqual(total['estimated_api_cost'],sum(row['estimated_api_cost'] for row in report['records']))
        self.assertEqual(total['usage']['completion_tokens']['observed_total'],612)


if __name__=='__main__':unittest.main()
