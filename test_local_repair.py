import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agent_runtime.local_repair import rounding_candidates
from agent_runtime.compact_patch import compile_patch
from test_v18_execution import PRICING_EDITS
from agent_runtime.repair_loop import tasks
from agent_runtime.runtime import run
from agent_runtime.model_gateway import ModelGateway


class LocalRepairTests(unittest.TestCase):
    def test_rules_are_generic_bounded_and_preserve_unrelated_functions(self):
        source='def arbitrary(a,b,c):\n return a-b//c\ndef untouched(x):\n return x+1\n'
        candidates=rounding_candidates(source,{'arbitrary'})
        self.assertEqual(len(candidates),1);self.assertIn('(a * c - b) // c',candidates[0]['source'])
        self.assertIn('return x + 1',candidates[0]['source'])
        self.assertEqual(rounding_candidates(source,{'other'}),[])
        self.assertEqual(rounding_candidates('def f(a,b):\n return a+b\n',{'f'}),[])
        with self.assertRaises(ValueError):rounding_candidates(source,{'arbitrary'},5)

    @unittest.skipUnless(os.name=='posix','actual protected tests')
    def test_one_failed_model_output_is_fixed_by_checked_local_tool_without_second_model_call(self):
        good=compile_patch(json.dumps(PRICING_EDITS),'pricing')
        bad=good.replace('unit_cents * quantity * (100 - discount_pct) // 100','unit_cents * quantity - unit_cents * quantity * discount_pct // 100')
        self.assertNotEqual(good,bad)
        calls=[]
        class Client:
            model='offline-fixed-model-output';timeout=3
            def complete(self,messages,max_tokens):
                calls.append(1);return {'content':json.dumps({'source':bad}),'finish_reason':'stop','usage':{}}
        with tempfile.TemporaryDirectory() as folder,patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda path:ModelGateway(Client(),path)):
            states,events=run(tasks('修复pricing并交付实际测试源码','pricing'),quiet=True,output_dir=Path(folder)/'runs',workspace=Path(folder)/'workspace',cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue(all(value=='succeeded' for value in states.values()))
            final=next(e['result']['result'] for e in events if e['state']=='succeeded' and e['task_id']=='repair_check_1')
            self.assertTrue(final['accepted']);self.assertEqual(len(calls),1)
            self.assertTrue(final['verification']['local_repair']['candidates'][-1]['success'])
            self.assertEqual(final['verification']['heldout']['failed'],0)
