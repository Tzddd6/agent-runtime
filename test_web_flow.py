import unittest
from unittest.mock import patch
from agent_runtime.web_flow import requested_urls,tasks,finish
from agent_runtime.routing import auto_tasks,repair_plan

class WebFlowTests(unittest.TestCase):
    def test_domain_protocol_punctuation_and_explicit_no_network(self):
        examples={'你去读下mc.tzddd.cn':['https://mc.tzddd.cn'],'读一下 https://www.python.org/。':['https://www.python.org/'],
                  '看看example.com和www.python.org':['https://example.com','https://www.python.org'],
                  '不要联网，读下example.com':[],'不需要工具，请看看example.com':[],'分析源代码':[]}
        for text,expected in examples.items():
            with self.subTest(text=text):self.assertEqual(requested_urls(text),expected)
    def test_short_url_request_skips_model_router(self):
        with patch('agent_runtime.computer.available',return_value=False):work=auto_tasks('你去读下mc.tzddd.cn')
        self.assertEqual(work[0]['plugin'],'web.fetch');self.assertEqual(work[0]['arguments']['url'],'https://mc.tzddd.cn')
        self.assertFalse(any(t['action']=='llm' for t in work))
    def test_http_auth_error_is_honest_and_spends_no_model_request(self):
        result,children=finish({'_inputs':{'r':{'plugin':'web.fetch','http_status':401,'url':'https://example.com/','text':'login'}}})
        self.assertFalse(children);self.assertIn('HTTP 401',result['answer']);self.assertEqual(result['termination'],'web_unavailable')
    def test_real_content_creates_only_one_summary_model_task(self):
        result,children=finish({'id':'gate','_inputs':{'r':{'plugin':'web.fetch','http_status':200,'url':'https://example.com/','text':'actual source'}}})
        self.assertEqual(len(children),1);self.assertEqual(children[0]['inputs'],['r']);self.assertEqual(children[0]['action'],'llm')
    def test_bounded_plan_repair_never_broadens_tool_permissions(self):
        result,children=repair_plan({'id':'route_gate','_inputs':{'route':'invalid'},'_contexts':[]},ValueError('插件任务无效'))
        self.assertTrue(result['repair_scheduled']);self.assertEqual(len(children),2);self.assertEqual(children[1]['plan_repairs'],1)
        final,children=repair_plan({'plan_repairs':1},ValueError('still invalid'))
        self.assertEqual(final['termination'],'invalid_plan');self.assertEqual(children,[])
