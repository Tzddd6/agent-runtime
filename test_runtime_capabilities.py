import json,os,tempfile,unittest
from unittest.mock import patch
from agent_runtime.runtime_capabilities import network_question
from agent_runtime.routing import auto_tasks,compact_catalog
from agent_runtime.runtime import run
from agent_runtime.chat_store import answer_for


class CapabilityTruthTests(unittest.TestCase):
    def test_capability_question_is_distinct_from_a_request_to_browse(self):
        for prompt in ('你能联网吗？','你有联网浏览能力吗','Agent Runtime是否支持上网？'):
            self.assertTrue(network_question(prompt),prompt)
        for prompt in ('你能联网帮我查一下今天新闻吗？','打开https://example.com/','你能搜索网页吗？'):
            self.assertFalse(network_question(prompt),prompt)
        self.assertIn('web.fetch',json.loads(compact_catalog('联网浏览')))

    def test_runtime_truth_answer_is_local_and_does_not_spend_or_claim_a_web_read(self):
        tasks=auto_tasks('你有联网浏览能力吗？')
        self.assertEqual(tasks[0]['action'],'capabilities')
        with tempfile.TemporaryDirectory() as folder,patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=AssertionError('No model call permitted')):
            states,events=run(tasks,quiet=True,output_dir=folder,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            answer=answer_for({'preset':'auto','status':'succeeded','events':events})
            self.assertIn('web.fetch',answer);self.assertIn('没有声称刚刚浏览',answer)
            self.assertTrue(all(value=='succeeded' for value in states.values()))
