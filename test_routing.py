import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent_runtime.routing import auto_tasks,route_plan
from agent_runtime.runtime import run
from agent_runtime.model_gateway import ModelGateway
from agent_runtime.chat_store import answer_for


class RoutingTests(unittest.TestCase):
    def test_large_plan_accepts_seven_tasks_and_rejects_thirteen(self):
        rows=[{'id':'t'+str(i),'plugin':'math.calculate','arguments':{'expression':'1+1'}} for i in range(13)]
        result,children=route_plan(self.task({'route':'collaborate','tasks':rows[:7]}))
        self.assertEqual(result['tasks'],7)
        self.assertEqual(len([t for t in children if t.get('plugin')]),7)
        with self.assertRaises(ValueError):route_plan(self.task({'route':'collaborate','tasks':rows}))

    def test_expected_baseline_failure_is_recorded_without_faking_success(self):
        task=self.task({'route':'direct','answer':'Observed failing baseline and repaired.'})
        task['_inputs']['baseline']={'plugin':'command.run','ok':False,'executed':True,'exit_code':1,'expected_failure_observed':True}
        result,_=route_plan(task);self.assertNotIn('termination',result)
        task['_inputs']['baseline'].pop('expected_failure_observed')
        result,_=route_plan(task);self.assertEqual(result['termination'],'tool_failures')
    def task(self,plan):
        return {'_inputs':{'route':json.dumps(plan)},'_contexts':[{'user_request':'fixture'}],'response_tokens':4096}

    def test_direct_and_collaborative_route_are_validated_before_spawn(self):
        result,children=route_plan(self.task({'route':'direct','answer':'hello'}))
        self.assertEqual(result['answer'],'hello');self.assertEqual(children,[])
        plan={'route':'collaborate','tasks':[{'id':'a','role':'analyst','instruction':'analyze'},
            {'id':'b','role':'tester','instruction':'test','depends_on':['a']}]}
        result,children=route_plan(self.task(plan))
        self.assertEqual(result['tasks'],2)
        self.assertEqual(children[1]['inputs'],['auto_a'])
        self.assertEqual(children[-1]['depends_on'],['auto_a','auto_b'])
        self.assertEqual(children[-1]['max_tokens'],4096)
        self.assertTrue(all(t['action']=='llm' and 'argv' not in t for t in children))

    def test_invalid_or_executable_plans_fail_closed(self):
        invalid=[{'route':'unknown'},{'route':'direct','answer':''},
            {'route':'collaborate','tasks':[{'id':'a','role':'shell','instruction':'x'}]},
            {'route':'collaborate','tasks':[{'id':'a','role':'coder','instruction':'x','argv':['sh']}]},
            {'route':'collaborate','tasks':[{'id':'a','role':'analyst','instruction':'x','depends_on':['a']}]},
            {'route':'collaborate','tasks':[{'id':'a','role':'analyst','instruction':'x','depends_on':['missing']}]},
            {'route':'collaborate','tasks':[{'id':'a','role':{},'instruction':'x'}]}]
        for plan in invalid:
            with self.subTest(plan=plan),self.assertRaises(ValueError):route_plan(self.task(plan))

    def test_real_workers_spawn_route_and_consume_upstream_with_mock_provider(self):
        requests=[]
        class Client:
            model='fixture';timeout=4
            def complete(self,messages,max_tokens):
                requests.append(messages)
                system=messages[0]['content']
                if '任务路由器' in system:
                    text=json.dumps({'route':'collaborate','reason':'parallel fixture','tasks':[
                        {'id':'analysis','role':'analyst','instruction':'analysis'},
                        {'id':'testing','role':'tester','instruction':'testing'}]})
                elif '负责分析' in system:text='ANALYSIS_WITNESS'
                elif '负责测试设计' in system:text='TEST_WITNESS'
                else:
                    self_check=json.loads(messages[-1]['content'])['upstream_results']
                    assert set(self_check.values())=={'ANALYSIS_WITNESS','TEST_WITNESS'}
                    text='verified final'
                return {'content':text,'usage':{}}
        with tempfile.TemporaryDirectory() as tmp,patch.object(ModelGateway,'from_env',side_effect=lambda path:ModelGateway(Client(),path)):
            states,events=run(auto_tasks('fixture'),quiet=True,output_dir=tmp,
                cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'),
                budget={'cpu_milli':2000,'memory_bytes':1024**3,'pids':128})
            self.assertTrue(all(v=='succeeded' for v in states.values()),states)
            self.assertEqual(len(requests),4)
            self.assertEqual(answer_for({'preset':'auto','status':'succeeded','events':events}),'verified final')
            self.assertIn('auto_answer',states)
            summary=json.loads(next(Path(tmp).glob('*/summary.json')).read_text())
            self.assertIn('analyst_analysis',[a['id'] for a in summary['agents']])
