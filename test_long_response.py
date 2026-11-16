import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agent_runtime.routing import auto_tasks
from agent_runtime.runtime import run
from agent_runtime.model_gateway import ModelGateway,ModelError
from agent_runtime.chat_store import answer_for

class LongResponseTests(unittest.TestCase):
    def test_explanation_uses_one_complete_answer_instead_of_three_short_agents(self):
        work=auto_tasks('详细介绍拉普拉斯变换')
        self.assertEqual(len(work),1);self.assertEqual(work[0]['max_tokens'],4096);self.assertEqual(work[0]['continuation_limit'],1)
        standalone=auto_tasks('详细说明傅里叶变换',conversation=[{'role':'user','content':'生成SVG'}],deep_thinking=True)
        self.assertEqual(standalone[0]['contexts'],[]);self.assertTrue(standalone[0]['thinking'])
        followup=auto_tasks('继续解释刚才的原理',conversation=[{'role':'user','content':'目标'}])
        self.assertTrue(followup[0]['contexts'])
    def test_confirmed_length_continues_once_counts_tokens_and_preserves_goal(self):
        calls=[]
        class Client:
            timeout=3;model='offline'
            def complete(self,messages,max_tokens):
                calls.append(messages)
                if len(calls)==1:return {'content':'开头'+'A'*40,'finish_reason':'length','usage':{'prompt_tokens':3,'completion_tokens':5}}
                assert '详细介绍拉普拉斯变换' in str(messages) and messages[-2]['role']=='assistant'
                return {'content':'A'*40+'结尾','finish_reason':'stop','usage':{'prompt_tokens':7,'completion_tokens':11}}
        with tempfile.TemporaryDirectory() as folder,patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda p:ModelGateway(Client(),p)):
            states,events=run(auto_tasks('详细介绍拉普拉斯变换'),quiet=True,output_dir=folder,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
        self.assertTrue(all(v=='succeeded' for v in states.values()));self.assertEqual(len(calls),2)
        result=next(e['result'] for e in events if e['state']=='succeeded')
        self.assertEqual(result['result'],'开头'+'A'*40+'结尾');self.assertEqual(result['model_call']['usage']['prompt_tokens'],10);self.assertEqual(result['model_call']['provider_calls'],2)
    def test_uncertain_error_never_continues_and_one_call_budget_blocks_paid_continuation(self):
        for mode in ('error','budget'):
            calls=[]
            class Client:
                timeout=3;model='offline'
                def complete(self,messages,max_tokens):
                    calls.append(1)
                    if mode=='error':raise ModelError('private sk-DO-NOT-EXPOSE')
                    return {'content':'incomplete','finish_reason':'length','usage':{}}
            with tempfile.TemporaryDirectory() as folder,patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda p:ModelGateway(Client(),p)):
                states,events=run(auto_tasks('详细介绍拉普拉斯变换'),quiet=True,output_dir=folder,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'),model_budget={'calls':1,'output_tokens':4096})
            self.assertEqual(len(calls),1);self.assertEqual(states['auto_answer'],'failed');self.assertNotIn('DO-NOT-EXPOSE',str(events))
    def test_svg_continuation_is_checked_and_does_not_claim_semantic_success(self):
        calls=[]
        class Client:
            timeout=3;model='offline'
            def complete(self,messages,max_tokens):
                calls.append(1)
                return {'content':'<svg xmlns="http://www.w3.org/2000/svg"><text>预算95' if len(calls)==1 else '</text></svg>',
                        'finish_reason':'length' if len(calls)==1 else 'stop','usage':{}}
        with tempfile.TemporaryDirectory() as folder,patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda p:ModelGateway(Client(),p)):
            states,events=run(auto_tasks('生成完整SVG图'),quiet=True,output_dir=Path(folder)/'runs',workspace=Path(folder)/'workspace',cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue((Path(folder)/'workspace/illustration.svg').is_file())
            self.assertTrue(all(v=='succeeded' for v in states.values()))
            answer=answer_for({'preset':'auto','status':'succeeded','events':events});self.assertIn('语义',answer)
    def test_large_artifact_allows_bounded_continuations_and_never_unbounded(self):
        from agent_runtime.model_exchange import complete
        from agent_runtime.runtime import validate
        large=auto_tasks('生成SVG图，完整内容：'+'细节'*800)
        self.assertEqual(large[0]['continuation_limit'],3)
        validate(large)
        for value in (4,-1,True):
            with self.assertRaises(ValueError):complete({'continuation_limit':value},[])
    def test_generated_svg_syntax_fix_is_narrow_and_does_not_relax_security(self):
        from agent_runtime.svg_artifact import normalize_generated_svg
        bad='<svg xmlns="http://www.w3.org/2000/svg"><rect x="0"" y="2"/></svg>'
        value,repairs=normalize_generated_svg(bad)
        self.assertIn('x="0" y="2"',value);self.assertEqual(repairs[0]['count'],1)
        for invalid in [bad.replace('<rect','<script/><rect'),'<svg>',bad.replace('x="0"" y="2"','href="https://example.com/"')]:
            with self.assertRaises(ValueError):normalize_generated_svg(invalid)
    def test_syntax_patch_only_binds_current_fault_and_validates_full_document(self):
        from agent_runtime.svg_workflow import apply_syntax_patch,finish
        text='<svg xmlns="http://www.w3.org/2000/svg">\n<line stroke="#<line x1="20"/>\n</svg>'
        fixed=apply_syntax_patch(text,json.dumps({'old':'<line stroke="#','new':'<line stroke="#666"/>'}))
        from agent_runtime.svg_artifact import validate_svg
        self.assertTrue(validate_svg(fixed)['xml_valid'])
        with self.assertRaises(ValueError):apply_syntax_patch(text,json.dumps({'old':'not-present','new':'drop'}))
        verdict,children=finish({'_inputs':{},'syntax_repairs':2})
        self.assertFalse(verdict['ok']);self.assertEqual(verdict['termination'],'tool_failures');self.assertEqual(children,[])
