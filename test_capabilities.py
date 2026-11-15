import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent_runtime.capabilities import calculate,execute_plugin,catalog,load_skill
from agent_runtime.routing import auto_tasks,route_plan
from agent_runtime.runtime import run
from agent_runtime.model_gateway import DeepSeekClient,THINKING_MARKER,ModelGateway


class CapabilityTests(unittest.TestCase):
    def test_skills_are_bounded_registered_instructions(self):
        for row in catalog()['skills']:self.assertTrue(load_skill(row['id']))
        for name in ('../secret','.env','unknown'):
            with self.assertRaises(ValueError):load_skill(name)

    def test_arithmetic_cannot_execute_code_or_allocate_unbounded_values(self):
        self.assertEqual(calculate('(12+3)*2/5'),6)
        for value in ('__import__("os")','2**100000','[0]*1000000','float("inf")','x+1','1e999'):
            with self.subTest(value=value),self.assertRaises((ValueError,SyntaxError)):calculate(value)

    def test_plugins_actually_compute_and_never_execute_python(self):
        result=execute_plugin({'plugin':'text.stats','arguments':{'text':'one two\nthree'}})
        self.assertEqual(result['words'],3)
        result=execute_plugin({'plugin':'python.syntax','arguments':{'source':'def foo():\n return 1\n'}})
        self.assertTrue(result['valid']);self.assertEqual(result['functions'],['foo']);self.assertFalse(result['code_executed'])
        self.assertFalse(execute_plugin({'plugin':'python.syntax','arguments':{'source':'def broken('}})['valid'])
        for project in ('pricing','intervals','text_normalization'):
            self.assertTrue(execute_plugin({'plugin':'project.inspect','arguments':{'project':project}})['source'])
        with self.assertRaises(ValueError):execute_plugin({'plugin':'project.inspect','arguments':{'project':'../../.env'}})

    def test_route_binds_tools_and_skill_without_model_permissions(self):
        plan={'route':'collaborate','tasks':[{'id':'calc','plugin':'math.calculate','arguments':{'expression':'6*7'}},
            {'id':'review','role':'reviewer','instruction':'verify','depends_on':['calc'],'skill':'test-design'}]}
        result,children=route_plan({'_inputs':{'route':json.dumps(plan)},'_contexts':[],'deep_thinking':True})
        self.assertEqual(children[0]['action'],'plugin');self.assertTrue(next(t for t in children if t['agent']=='synthesizer')['thinking'])
        self.assertIn('测试与验收',children[1]['messages'][0]['content'])
        self.assertEqual(result['plugins'],['math.calculate'])
        plan['tasks'][1]['skill']='../../secret'
        with self.assertRaises(ValueError):route_plan({'_inputs':{'route':json.dumps(plan)}})

    def test_native_thinking_is_an_api_parameter_and_marker_is_removed(self):
        seen=[]
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        class Opener:
            def open(self,req,timeout):
                seen.append(json.loads(req.data));return Response(json.dumps({'choices':[{'message':{'content':'answer','reasoning_content':'DO_NOT_EXPORT'},'finish_reason':'stop'}],'usage':{}}).encode())
        client=DeepSeekClient('fixture-key-not-live')
        with patch('agent_runtime.model_gateway.urllib.request.build_opener',return_value=Opener()):
            result=client._complete_direct([{'role':'system','content':THINKING_MARKER},{'role':'user','content':'fixture'}])
            client._complete_direct([{'role':'user','content':THINKING_MARKER}])
        self.assertEqual(seen[0]['thinking'],{'type':'enabled'});self.assertEqual(seen[0]['reasoning_effort'],'high')
        self.assertEqual(len(seen[0]['messages']),1);self.assertEqual(seen[1]['thinking'],{'type':'disabled'})
        self.assertNotIn('DO_NOT_EXPORT',json.dumps(result))

    def test_real_worker_tool_result_is_consumed_by_final_model_with_fixture(self):
        class Client:
            timeout=3;model='fixture'
            def complete(self,messages,max_tokens):
                if '任务路由器' in messages[0]['content']:
                    answer=json.dumps({'route':'collaborate','tasks':[{'id':'calc','plugin':'math.calculate','arguments':{'expression':'6*7'}}]})
                else:
                    upstream=json.loads(messages[-1]['content'])['upstream_results']['auto_calc']
                    assert upstream['value']==42 and upstream['executed'] is True
                    answer=json.dumps({'route':'direct','answer':'42 from actual tool'})
                return {'content':answer,'usage':{}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(ModelGateway,'from_env',side_effect=lambda path:ModelGateway(Client(),path)):
            states,events=run(auto_tasks('fixture'),quiet=True,output_dir=tmp,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue(all(s=='succeeded' for s in states.values()),states)
            result=next(e['result']['result'] for e in events if e['task_id']=='auto_calc' and e['state']=='succeeded')
            self.assertEqual(result['value'],42)
