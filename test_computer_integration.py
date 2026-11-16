import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agent_runtime.computer import tasks
from agent_runtime.runtime import run
from agent_runtime.model_gateway import ModelGateway

@unittest.skipUnless(os.environ.get('AGENTRT_TEST_BROWSER')=='1','explicit real browser sandbox acceptance')
class ComputerIntegrationTests(unittest.TestCase):
    def test_actual_desktop_click_type_observe_and_cgroup_cleanup(self):
        calls=[]
        class Client:
            timeout=3;model='offline-desktop-controller'
            def complete(self,messages,max_tokens):
                data=json.loads(messages[-1]['content']);view=data['observation'];history=data['previous_actions'];calls.append(1)
                if 'PINE-442：预算95元，负责人Cedar。' in view['text']:action={'action':'done','answer':'已查询PINE-442，预算95元，负责人Cedar。'}
                elif history and history[-1]['action']=='type':action={'action':'click','ref':next(e['ref'] for e in view['elements'] if e['label']=='查询资料'),'snapshot':view['id']}
                else:action={'action':'type','ref':next(e['ref'] for e in view['elements'] if e['tag']=='input'),'snapshot':view['id'],'text':'PINE-442'}
                return {'content':json.dumps(action,ensure_ascii=False),'usage':{},'finish_reason':'stop'}
        with tempfile.TemporaryDirectory() as folder,patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda path:ModelGateway(Client(),path)):
            states,events=run(tasks('打开电脑实验桌面，查询PINE-442'),quiet=True,cgroup_root=os.environ['AGENTRT_TEST_CGROUP'],output_dir=Path(folder)/'runs',workspace=Path(folder)/'workspace',budget={'cpu_milli':2000,'memory_bytes':1024**3,'pids':224})
            self.assertEqual(states,{'auto_answer':'succeeded'})
            result=next(e['result']['result'] for e in events if e['state']=='succeeded')
            self.assertTrue(result['ok'],result);self.assertEqual(len(calls),3)
            self.assertIn('PINE-442：预算95元，负责人Cedar。',result['observation']['text'])
            self.assertEqual([s['action'] for s in result['steps']],['type','click','done'])
            self.assertTrue((Path(folder)/'workspace'/result['artifacts'][0]['path']).read_bytes().startswith(b'\xff\xd8\xff'))
