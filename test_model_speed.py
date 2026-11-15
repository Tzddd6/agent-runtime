"""Offline coverage for larger tasks, compact collaboration and redacted timings."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent_runtime.chat_store import ChatStore,history_messages
from agent_runtime.conversation import review_tasks,route_review
from agent_runtime.model_budget import ModelBudgetLedger,DEFAULT_WORKFLOW_MODEL_BUDGET
from agent_runtime.model_broker import BrokerClient
from agent_runtime.model_gateway import public_timings
from agent_runtime.scenarios import team_tasks
from agent_runtime.web import Controller


class ModelSpeedTests(unittest.TestCase):
    def test_compact_and_large_team_keep_identical_dependency_edges(self):
        compact=team_tasks('fixture');large=team_tasks('fixture',final_tokens=4096)
        self.assertEqual([(t['id'],t['depends_on'],t['inputs']) for t in compact],
                         [(t['id'],t['depends_on'],t['inputs']) for t in large])
        self.assertEqual([t['max_tokens'] for t in compact],[256,256,256,512])
        self.assertEqual([t['max_tokens'] for t in large],[1024,1024,1024,4096])

    def test_larger_budget_remains_enforced_and_nonrefundable(self):
        ledger=ModelBudgetLedger();ledger.register('owner')
        for _ in range(16):ledger.admit('owner',4096)
        self.assertEqual(ledger.snapshot('owner')['used']['output_tokens'],65536)
        from agent_runtime.model_budget import ModelBudgetExceeded
        with self.assertRaises(ModelBudgetExceeded):ledger.admit('owner',1)
        self.assertEqual(DEFAULT_WORKFLOW_MODEL_BUDGET,{'calls':128,'output_tokens':262144})

    def test_larger_conversation_and_dedup_include_response_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ChatStore(tmp);key='a'*32
            for i in range(25):
                request=f'{i:032x}';store.intent(key,request,'x'*5000,'llm','pricing',{})
                store.update(key,request,status='succeeded',answer='y'*5000,job_id='fixture')
            chat=store.read(key);self.assertEqual(len(chat['turns']),25)
            messages=history_messages({'turns':chat['turns']+[{}]})
            self.assertGreater(len(messages),12);self.assertLessEqual(len(messages),32)
            self.assertLessEqual(len(json.dumps(messages,ensure_ascii=False).encode()),98304)
            with self.assertRaises(ValueError):store.intent(key,f'{24:032x}','x'*5000,'llm','pricing',{},response_tokens=512)

    def test_revisions_preserve_large_response_limit(self):
        gate=review_tasks('fixture',response_tokens=4096)[-1]
        gate['_inputs']={'writer-1':'draft','critic-1':json.dumps({'decision':'revise','feedback':'improve'})}
        _,children=route_review(gate)
        self.assertEqual(children[0]['max_tokens'],4096)

    def test_timing_fields_cannot_log_credentials_or_unknown_fields(self):
        self.assertEqual(public_timings({'client_seconds':.3,'broker_seconds':'SECRET',
            'key':'SECRET','status_reads':3,'completion_detection_seconds':float('inf')}),
            {'client_seconds':.3,'broker_seconds':None,'status_reads':3,'completion_detection_seconds':None})

    def test_client_keeps_one_paid_submission_and_reports_numeric_timings(self):
        client=BrokerClient('fixture-control-token');paths=[]
        def request(path,body=None):
            paths.append(path)
            if path=='/calls':return {'call_id':'a'*32}
            return {'status':'succeeded','created':10,'finished':11,
                    'result':{'content':'fixture','usage':{}}}
        with patch.object(client,'_request',side_effect=request):result=client.complete([{'role':'user','content':'fixture'}])
        self.assertEqual(paths,['/calls','/calls/'+'a'*32])
        self.assertEqual(result['transport_timings']['status_reads'],1)
        self.assertEqual(result['transport_timings']['broker_seconds'],1)

    def test_invalid_response_limit_rejected_before_persisting_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Controller(tmp)
            for limit in (True,255,4097):
                with self.assertRaises(ValueError):c.submit_chat({'conversation_id':'a'*32,
                    'request_id':'b'*32,'mode':'llm','prompt':'fixture','response_tokens':limit})
            self.assertEqual(c.chats.listing(),[])
