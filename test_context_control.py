import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent_runtime.context import ContextStore
from agent_runtime.context_control import ContextGovernor,encoded
from agent_runtime.ipc import Broker


class ContextControlTests(unittest.TestCase):
    def test_shipped_mailbox_example_runs_and_preserves_both_claims(self):
        from agent_runtime.runtime import run
        tasks=json.loads((Path(__file__).parent/'examples/context-sharing.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as tmp:
            states,events=run(tasks,quiet=True,output_dir=tmp,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue(all(state=='succeeded' for state in states.values()),states)
            outcome=next(e['result']['result'] for e in events if e['task_id']=='inspect_conflict' and e['state']=='succeeded')
            self.assertEqual({item['value'] for item in outcome['conflicts'][0]['alternatives']},{2,3})

    def test_small_inputs_remain_exact_and_duplicate_contexts_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ContextStore(Path(tmp));ref=store.put({'user_request':'保留接口','facts':[1,2]},['a'])
            governor=ContextGovernor(store);view=governor.view('a',[ref,ref],{'one':ref})
            self.assertEqual(view['body']['context'],[store.get(ref,'a')]);self.assertEqual(view['body']['upstream_results']['one'],store.get(ref,'a'))
            self.assertEqual(governor.stats()['deduplicated_objects'],1)

    def test_projection_keeps_goal_constraints_relevant_span_and_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ContextStore(Path(tmp));goal={'user_request':'找出 WITNESS 最终编号，必须保留测试，不得泄露密钥。','constraints':['不要修改测试']}
            request=store.put(goal,['a']);text=('背景说明 irrelevant filler.\n'*4000)+'\nWITNESS=FINAL_7319\n'
            source=store.put(text,['a']);g=ContextGovernor(store,soft_bytes=6000)
            view=g.view('a',[request],{'analysis':source},'WITNESS')
            body=view['body'];self.assertEqual(body['context'][0]['user_request'],goal['user_request'])
            self.assertEqual(body['context'][0]['constraints'],goal['constraints']);self.assertIn('FINAL_7319',json.dumps(body))
            self.assertLessEqual(view['stats']['sent_text_bytes'],6000);self.assertLess(view['stats']['sent_text_bytes'],view['stats']['raw_text_bytes']/4)
            self.assertEqual(store.get(source,'a'),text)

    def test_authorization_is_checked_before_cached_semantic_views(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ContextStore(Path(tmp));ref=store.put({'secret':'only a'},['a']);g=ContextGovernor(store)
            g.view('a',[],{'data':ref})
            with self.assertRaises(PermissionError):g.view('b',[],{'data':ref})
            self.assertEqual(g.search('b','only')['matches'],[])
            store.grant(ref,'a','b');self.assertIn('only a',g.read('b',ref)['text'])
            store.revoke_agent('b')
            with self.assertRaises(PermissionError):g.read('b',ref)

    def test_batch_is_all_or_nothing_and_digest_is_not_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ContextStore(Path(tmp));one=store.put('one',['a']);two=store.put('two',['b'])
            with self.assertRaises(PermissionError):store.get_many([one,two],'a')
            with self.assertRaises(PermissionError):store.grant(two,'a','c')

    def test_shared_claim_conflict_keeps_both_sources_and_not_truth_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ContextStore(Path(tmp))
            for owner,value in [('analyst',2),('reviewer',3)]:
                ref=store.put({'semantic_facts':[{'subject':'deployment','predicate':'workers','value':value}]},[owner,'consumer'])
                store.label_source(ref,{'agent':owner,'kind':'model_claim','action':'llm'})
            result=ContextGovernor(store).search('consumer','deployment workers')
            self.assertEqual(len(result['conflicts']),1);self.assertEqual(len(result['conflicts'][0]['alternatives']),2)
            self.assertTrue(all(r['provenance'][0]['kind']=='model_claim' for r in result['matches']))
            self.assertNotIn('verified_fact',json.dumps(result))

    def test_paging_preserves_original_and_enforces_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ContextStore(Path(tmp));ref=store.put('abcdef'*2000,['a']);g=ContextGovernor(store)
            parts=[];offset=0
            while True:
                page=g.read('a',ref,offset,1000);parts.append(page['text'])
                if page['next_offset'] is None:break
                offset=page['next_offset']
            self.assertEqual(json.loads(''.join(parts)),store.get(ref,'a'))
            with self.assertRaises(ValueError):g.read('a',ref,0,8001)

    def test_soft_overflow_preserves_goal_hard_overflow_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ContextStore(Path(tmp));ref=store.put({'user_request':'重要目标'*2000},['a']);g=ContextGovernor(store,soft_bytes=4096,hard_bytes=32768)
            self.assertEqual(g.view('a',[ref],{})['body']['context'][0]['user_request'],'重要目标'*2000)
            ref=store.put({'user_request':'重要目标'*4000},['a'])
            with self.assertRaisesRegex(ValueError,'未截断目标'):g.view('a',[ref],{})

    def test_integrity_failure_is_not_hidden_by_semantic_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ContextStore(Path(tmp));ref=store.put('marker alpha',['a']);g=ContextGovernor(store);g.search('a','marker')
            (Path(tmp)/ref).write_text('corrupted')
            with self.assertRaises(ValueError):g.search('a','marker')

    def test_image_channel_is_not_repeated_as_semantic_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ContextStore(Path(tmp));image={'mime':'image/jpeg','data':'fixture-only'}
            ref=store.put({'user_request':'识图','_images':[image]},['a']);view=ContextGovernor(store).view('a',[ref],{})
            self.assertEqual(view['images'],[image]);self.assertNotIn('fixture-only',json.dumps(view['body']))

    def test_authenticated_context_operations_cannot_override_budget_or_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ContextStore(Path(tmp));ref=store.put('WITNESS=73',['a']);broker=Broker(store,['a','b'])
            try:
                a=broker.issue('a');b=broker.issue('b')
                self.assertIn('73',broker.dispatch(a,{'op':'context_read','ref':ref,'agent':'b'})['text'])
                with self.assertRaises(PermissionError):broker.dispatch(b,{'op':'context_read','ref':ref,'agent':'a'})
                broker.revoke(a)
                with self.assertRaises(PermissionError):broker.dispatch(a,{'op':'context_read','ref':ref})
            finally:broker.close()

    def test_context_remainder_index_never_poison_full_source_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ContextStore(Path(tmp));ref=store.put({'user_request':'GOAL_NONCE_17','notes':'background '*5000},['a']);g=ContextGovernor(store)
            g.view('a',[ref],{},'background')
            self.assertTrue(any('GOAL_NONCE_17' in row['text'] for row in g.search('a','GOAL_NONCE_17')['matches']))

    def test_real_workers_share_semantic_facts_only_after_explicit_delegation(self):
        from agent_runtime.runtime import run
        value={'semantic_facts':[{'subject':'deploy','predicate':'workers','value':2}]}
        ref=hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        tasks=[{'id':'producer','agent':'writer','action':'echo','message':value},
               {'id':'gate','agent':'delegator','action':'echo','depends_on':['producer'],'inputs':['producer'],
                'spawn':[{'id':'reader','agent':'reader','action':'plugin','plugin':'context.search','arguments':{'query':'deploy workers'},'depends_on':['gate'],'_shared_refs':[ref]}]},
               {'id':'outsider','agent':'outsider','action':'plugin','plugin':'context.search','arguments':{'query':'deploy workers'}}]
        with tempfile.TemporaryDirectory() as tmp:
            states,events=run(tasks,quiet=True,output_dir=tmp,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue(all(s=='succeeded' for s in states.values()),states)
            results={e['task_id']:e['result']['result'] for e in events if e['state']=='succeeded'}
            self.assertEqual(results['outsider']['matches'],[])
            self.assertEqual(results['reader']['matches'][0]['source_ref'],ref)
            self.assertEqual(results['reader']['matches'][0]['provenance'][0]['agent'],'writer')

    def test_forged_delegation_fails_without_stopping_independent_agent(self):
        from agent_runtime.runtime import run
        value='private-context';ref=hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        tasks=[{'id':'producer','action':'echo','message':value},
               {'id':'bad_gate','action':'echo','depends_on':['producer'],'spawn':[{'id':'leak','action':'echo','depends_on':['bad_gate'],'_shared_refs':[ref]}]},
               {'id':'healthy','action':'echo','message':'still running'}]
        with tempfile.TemporaryDirectory() as tmp:
            states,_=run(tasks,quiet=True,output_dir=tmp,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertEqual(states['bad_gate'],'failed');self.assertEqual(states['healthy'],'succeeded');self.assertNotIn('leak',states)

    def test_real_model_worker_uses_bounded_view_and_preserves_evidence(self):
        from agent_runtime.runtime import run
        from agent_runtime.model_gateway import ModelGateway
        text='irrelevant background '*4000+'\nSHARED_NONCE_81 = 7201\n'
        class Client:
            model='offline';timeout=2
            def complete(self,messages,*args):
                if '7201' not in json.dumps(messages):raise AssertionError('required evidence dropped')
                return {'content':'SHARED_NONCE_81 = 7201','usage':{}}
        with tempfile.TemporaryDirectory() as tmp,patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda p:ModelGateway(Client(),p)):
            tasks=[{'id':'producer','action':'echo','message':text},{'id':'consumer','action':'llm','depends_on':['producer'],'inputs':['producer'],
                'contexts':[{'user_request':'读取SHARED_NONCE_81，不修改测试'}],'context_query':'SHARED_NONCE_81','messages':[{'role':'user','content':'What is SHARED_NONCE_81?'}]}]
            states,_=run(tasks,quiet=True,output_dir=tmp,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue(all(s=='succeeded' for s in states.values()),states)
            summary=json.loads(next(Path(tmp).glob('*/summary.json')).read_text())
            self.assertGreater(summary['context_control']['raw_text_bytes'],summary['context_control']['sent_text_bytes']*3)

    @unittest.skipUnless(os.name=='posix','private object permissions and safe tools require Linux')
    def test_private_objects_and_verified_patch_download_receipt(self):
        from agent_runtime.capabilities import execute_plugin
        from agent_runtime.engineering import BASELINE_SOURCE
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=ContextStore(root/'objects');ref=store.put('private',['a'])
            self.assertEqual((root/'objects').stat().st_mode&0o777,0o700)
            self.assertEqual((root/'objects'/ref).stat().st_mode&0o777,0o600)
            source=BASELINE_SOURCE.replace('unit_cents * quantity - discount_pct','unit_cents * quantity * (100 - discount_pct) // 100').replace('subtotal_cents > 5000','subtotal_cents >= 5000').replace('paid_cents // total_qty * returned_qty','paid_cents * returned_qty // total_qty')
            task={'plugin':'project.patch_check','arguments':{'project':'integer-pricing','source':source},'_workspace':str(root/'attempt'),'_project_workspace':str(root/'project')}
            result=execute_plugin(task)
            self.assertTrue(result['success']);self.assertTrue(result['written'])
            self.assertEqual(hashlib.sha256((root/'project'/result['path']).read_bytes()).hexdigest(),result['source_sha256'])
            self.assertEqual(execute_plugin({'plugin':'project.inspect','arguments':{'project':'time-intervals'}})['project'],'intervals')

    def test_empty_claim_contract_is_isolated_before_publication(self):
        from agent_runtime.runtime import run
        tasks=[{'id':'empty','action':'echo','message':{'semantic_facts':[]},'result_contract':'semantic_facts'},
               {'id':'dependent','action':'echo','depends_on':['empty'],'inputs':['empty']},
               {'id':'healthy','action':'echo','message':'independent'}]
        with tempfile.TemporaryDirectory() as tmp:
            states,events=run(tasks,quiet=True,output_dir=tmp,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertEqual(states,{'empty':'failed','dependent':'skipped','healthy':'succeeded'})
            self.assertTrue(any('空数组' in (e.get('error') or '') for e in events))

    def test_claim_contract_schema_keeps_uncertainty_explicit(self):
        from agent_runtime.contracts import validate_result
        validate_result('semantic_facts',json.dumps({'semantic_facts':[{'subject':'deploy','predicate':'workers','value':2}]}))
        for value in [{'semantic_facts':[]},{'semantic_facts':[{'subject':'x','predicate':'y','value':{'nested':2}}]},'not json']:
            with self.assertRaises(ValueError):validate_result('semantic_facts',value)


if __name__=='__main__':unittest.main()
