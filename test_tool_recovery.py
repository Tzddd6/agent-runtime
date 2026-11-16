import json,os,tempfile,unittest
from unittest.mock import patch
from agent_runtime.runtime import run
from agent_runtime.routing import auto_tasks
from agent_runtime.model_gateway import ModelGateway
from agent_runtime.execution_view import execution_view

class ToolRecoveryTests(unittest.TestCase):
    def test_invalid_arithmetic_is_observed_corrected_and_not_hidden(self):
        calls=[]
        class Client:
            model='offline-recovery';timeout=3
            def complete(self,messages,max_tokens):
                calls.append(messages)
                if len(calls)==1:value={'route':'collaborate','tasks':[{'id':'calc','plugin':'math.calculate','arguments':{'expression':'min over N guarantee check'}}]}
                elif len(calls)==2:
                    body=json.loads(messages[-1]['content']);error=body['upstream_results']['auto_calc']
                    assert error['ok'] is False and error['error_code']=='TOOL_INVALID_ARGUMENTS'
                    value={'route':'collaborate','tasks':[{'id':'fixed','plugin':'math.calculate','arguments':{'expression':'(199*35*90)//100+399'}}]}
                else:
                    assert json.loads(messages[-1]['content'])['upstream_results']['auto_r2_fixed']['value']==6667
                    value={'route':'direct','answer':'6667'}
                return {'content':json.dumps(value),'finish_reason':'stop','usage':{}}
        with tempfile.TemporaryDirectory() as folder,patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda path:ModelGateway(Client(),path)):
            states,events=run(auto_tasks('计算商品金额，必须使用工具并核对'),quiet=True,output_dir=folder,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
        self.assertTrue(all(state=='succeeded' for state in states.values()));self.assertEqual(len(calls),3)
        view=execution_view({'created':events[0]['time'],'status':'succeeded','preset':'auto','events':events})
        step=next(s for s in view['steps'] if s['id']=='auto_calc' and s['attempt']==1)
        self.assertEqual(step['status'],'failed');self.assertEqual(step['execution_status'],'succeeded')
        self.assertEqual(step['failure']['code'],'TOOL_INVALID_ARGUMENTS')
        self.assertEqual(next(e['result']['result']['answer'] for e in reversed(events) if e['state']=='succeeded' and e['task_id']=='auto_cycle_2'),'6667')
