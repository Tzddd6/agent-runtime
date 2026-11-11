import copy
import json
from pathlib import Path
import tempfile
import unittest
from tools.check_collaboration_evidence import check_tag_flow,check_review,model_usage
from tools.replay_project_cache import replay


def detail_for_team():
    tags={'plan':'Pabc1234','analyze':'Aabc1234','test_plan':'Tabc1234','review':'Rabc1234'}
    deps={'plan':[],'analyze':['plan'],'test_plan':['plan'],'review':['analyze','test_plan']}
    events=[]
    for index,task in enumerate(tags):
        events.extend([{'task_id':task,'state':'running','time':index*2},
          {'task_id':task,'state':'succeeded','time':index*2+1,'result':{'result':json.dumps({
            'tag':tags[task],'received_tags':[tags[d] for d in deps[task]],'answer':'role answer'}),
            'model_call':{'model':'fixture','latency_seconds':.1,
                          'usage':{'prompt_tokens':10,'completion_tokens':5}}}}])
    return {'events':events,'states':{task:'succeeded' for task in tags}}


class EvidenceTests(unittest.TestCase):
    def test_actual_upstream_tags_and_dependency_order_are_required(self):
        detail=detail_for_team()
        result=check_tag_flow(detail)
        self.assertTrue(result['passed'],result)
        self.assertEqual(result['usage']['totals']['prompt_tokens'],40)
        wrong=copy.deepcopy(detail)
        wrong['events'][-1]['result']['result']=json.dumps({'tag':'Rabc1234','received_tags':['Pabc1234'],'answer':'not upstream'})
        self.assertFalse(check_tag_flow(wrong)['passed'])
        wrong=copy.deepcopy(detail);wrong['events'][-2]['time']=0
        self.assertFalse(check_tag_flow(wrong)['passed'])

    def test_success_without_receipts_is_not_evidence(self):
        detail=detail_for_team()
        for event in detail['events']:
            if event['state']=='succeeded':event['result']['result']='looks correct'
        result=check_tag_flow(detail)
        self.assertFalse(result['passed'])
        self.assertIn('not proof',result['claim_boundary'])
        malformed=detail_for_team()
        malformed['events'][-1]['result']['result']=json.dumps({'tag':'Rabc1234','received_tags':[None,1],'answer':'x'})
        self.assertFalse(check_tag_flow(malformed)['passed'])

    def test_review_reports_only_observed_live_paths(self):
        events=[{'task_id':'writer-1','state':'succeeded','result':{'result':'answer'}},
                {'task_id':'critic-1','state':'succeeded','result':{'result':json.dumps({'decision':'approved','feedback':'ok'})}},
                {'task_id':'gate-1','state':'succeeded','result':{'result':{
                    'round':1,'termination':'approved','accepted':True}}}]
        detail={'events':events,'states':{e['task_id']:'succeeded' for e in events}}
        result=check_review(detail)
        self.assertTrue(result['passed'],result)
        self.assertTrue(result['observed_approved'])
        self.assertFalse(result['observed_revision'])
        self.assertFalse(result['observed_max_rounds'])
        events[-1]['result']['result'].update(termination='revision_requested',accepted=False)
        self.assertFalse(check_review(detail)['passed'])

    def test_fixed_output_replay_never_bills_and_tests_same_source(self):
        from agent_runtime.engineering import BASELINE_SOURCE
        calls=[]
        def checker(source,directory):
            calls.append(source)
            return {'success':False,'public':{'passed':0,'failed':1},'heldout':{'passed':0,'failed':1}}
        with tempfile.TemporaryDirectory() as directory:
            report=replay([{'source':BASELINE_SOURCE},{'analysis':'actual saved model output'}],
                {'source':BASELINE_SOURCE},Path(directory)/'replay',rounds=2,repeats=1,checker=checker)
        self.assertEqual(report['billed_model_calls'],0)
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0],calls[1])
        self.assertEqual(report['medians']['disabled']['disk_reads'],16)
        self.assertEqual(report['medians']['lru2']['disk_reads'],2)
        self.assertTrue(all(not r['success'] for r in report['records']))

    def test_replay_executes_real_immutable_tests_in_both_modes(self):
        from agent_runtime.engineering import BASELINE_SOURCE
        source=(BASELINE_SOURCE.replace('unit_cents * quantity - discount_pct',
                                        'unit_cents * quantity * (100 - discount_pct) // 100')
                               .replace('subtotal_cents > 5000','subtotal_cents >= 5000')
                               .replace('paid_cents // total_qty * returned_qty',
                                        'paid_cents * returned_qty // total_qty'))
        with tempfile.TemporaryDirectory() as directory:
            report=replay([{'source':source}],{'source':source},
                          Path(directory)/'real-check',rounds=1,repeats=1)
        for row in report['records']:
            self.assertTrue(row['success'],row['test_results'])
            self.assertGreater(row['test_results']['baseline_failing_count'],0)
            self.assertEqual(row['test_results']['public']['failed'],0)
            self.assertEqual(row['test_results']['heldout']['failed'],0)
            self.assertEqual(row['billed_model_calls'],0)


if __name__=='__main__':unittest.main()
