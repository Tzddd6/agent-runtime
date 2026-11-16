import json
import os
from pathlib import Path
import tempfile
import unittest
from agent_runtime.capabilities import execute_plugin
from agent_runtime.svg_artifact import validate_svg
from agent_runtime.tooling import operate
from agent_runtime.routing import route_plan
from agent_runtime.runtime import run

SVG='<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect id="bar-A" x="5" y="40" width="20" height="60"/></svg>'


class ArtifactBindingTests(unittest.TestCase):
    def test_svg_rejects_placeholders_code_injection_external_and_infinite_recursion(self):
        invalid=['待该任务执行前需由上一步提供内容','<svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/image"/></svg>',
            '<svg xmlns="http://www.w3.org/2000/svg"><g id="a"><use href="#a"/></g></svg>',
            '<!DOCTYPE svg [<!ENTITY x "oops">]>'+SVG]
        for text in invalid:
            with self.subTest(text=text),self.assertRaises(ValueError):validate_svg(text)
        self.assertTrue(validate_svg(SVG)['xml_valid'])
        self.assertFalse(validate_svg(SVG)['semantic_constraints_verified'])
        self.assertTrue(validate_svg('<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="g"/></defs><rect fill="url(\'#g\')"/></svg>')['standalone'])

    @unittest.skipUnless(os.name=='posix','requires project file operations')
    def test_writer_binds_only_declared_authorized_input_and_checks_svg_before_write(self):
        with tempfile.TemporaryDirectory() as folder:
            task={'plugin':'file.write','arguments':{'path':'chart.svg','content_from':'draft'},
                  '_project_workspace':folder,'inputs':['draft'],'_inputs':{'draft':'```svg\n'+SVG+'\n```'}}
            result=execute_plugin(task);self.assertTrue(result['svg_validation']['xml_valid'])
            self.assertEqual((Path(folder)/'chart.svg').read_text(),SVG)
            with self.assertRaises(ValueError):execute_plugin({**task,'inputs':[]})
            with self.assertRaises(ValueError):execute_plugin({**task,'arguments':{**task['arguments'],'content':'placeholder'}})
            with self.assertRaises(ValueError):operate(task,'file.write',{'path':'bad.svg','content':'待提供'})
            self.assertFalse((Path(folder)/'bad.svg').exists())

    def test_router_rewrites_binding_id_and_rejects_unrelated_agent_input(self):
        plan={'route':'collaborate','tasks':[{'id':'draft','role':'coder','instruction':'generate SVG','depends_on':[]},
             {'id':'save','plugin':'file.write','arguments':{'path':'chart.svg','content_from':'draft'},'depends_on':['draft']}]}
        task={'id':'route_gate','_inputs':{'route':json.dumps(plan)},'_contexts':[]}
        result,children=route_plan(task)
        save=next(child for child in children if child['id']=='auto_save')
        self.assertEqual(save['arguments']['content_from'],'auto_draft')
        self.assertIn('auto_draft',save['inputs'])
        plan['tasks'][1]['depends_on']=[]
        with self.assertRaises(ValueError):route_plan({**task,'_inputs':{'route':json.dumps(plan)}})

    def test_final_model_cannot_override_a_real_tool_failure(self):
        result,children=route_plan({'_inputs':{'route':'{"route":"direct","answer":"全部完成"}',
            'write':{'plugin':'file.write','ok':False,'executed':False}},'_contexts':[]})
        self.assertEqual(result['termination'],'tool_failures');self.assertNotIn('全部完成',result['answer'])

    @unittest.skipUnless(os.name=='posix','requires real Worker file IPC')
    def test_real_agents_write_exact_upstream_artifact_through_authorized_refs(self):
        with tempfile.TemporaryDirectory() as folder:
            states,events=run([{'id':'draft','agent':'author','message':SVG},
                {'id':'save','agent':'publisher','action':'plugin','plugin':'file.write',
                 'arguments':{'path':'chart.svg','content_from':'draft'},'depends_on':['draft'],'inputs':['draft']}],
                quiet=True,output_dir=Path(folder)/'runs',workspace=Path(folder)/'workspace',
                cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertEqual(states,{'draft':'succeeded','save':'succeeded'})
            self.assertEqual((Path(folder)/'workspace/chart.svg').read_text(),SVG)


if __name__=='__main__':unittest.main()
