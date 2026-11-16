import importlib.util,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agent_runtime.tau_adapter import TauBridge,EvaluationBudget
from agent_runtime.model_gateway import ModelGateway


class FixedNativeClient:
    model='fixed-native';timeout=5
    def __init__(self):self.calls=[]
    def complete(self,messages,max_tokens):
        self.calls.append(messages)
        message={'role':'assistant','content':None,'tool_calls':[{'id':'c1','type':'function','function':{'name':'lookup','arguments':'{"id": "123"}'}}]}
        return {'content':json.dumps({'message':message}),'finish_reason':'tool_calls','model':self.model,'usage':{'prompt_tokens':100,'completion_tokens':7}}


class BudgetTests(unittest.TestCase):
    def test_nonfinite_limits_rejected(self):
        for value in (float('nan'),float('inf'),-1,0,True):
            with self.assertRaises(ValueError):EvaluationBudget(value,2)

    @unittest.skipUnless(os.name=='posix','requires flock')
    def test_corrupt_nan_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=Path(folder)/'budget.json'
            ledger.write_text(json.dumps({'limit_cny':10.,'spent_or_reserved_cny':float('nan'),'calls':0}))
            with self.assertRaises(ValueError):EvaluationBudget(10.,4,ledger).reserve(256)
    @unittest.skipUnless(os.name=='posix','requires flock')
    def test_new_process_budget_object_cannot_reset_shared_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=Path(folder)/'budget.json'
            first=EvaluationBudget(3.,5,ledger);first.reserve(256)
            second=EvaluationBudget(3.,5,ledger)
            with self.assertRaises(RuntimeError):second.reserve(256)
            self.assertEqual(json.loads(ledger.read_text())['calls'],1)
    def test_reserved_peak_bound_and_uncertain_call_not_refunded(self):
        budget=EvaluationBudget(2.2,2);reserve=budget.reserve(256)
        self.assertIsNone(budget.settle(reserve,{}))
        with self.assertRaises(RuntimeError):budget.reserve(256)

    def test_cache_accounting(self):
        budget=EvaluationBudget(10.,4);reserve=budget.reserve(256)
        cost=budget.settle(reserve,{'prompt_tokens':100000,'prompt_cache_hit_tokens':90000,'completion_tokens':1000})
        self.assertAlmostEqual(cost,.0316)


@unittest.skipUnless(importlib.util.find_spec('tau2'),'requires isolated official tau2 environment')
class TauAdapterTests(unittest.TestCase):
    def test_budget_refusal_flag_prevents_incomplete_domain_claim(self):
        from tau2.data_model.message import UserMessage
        with tempfile.TemporaryDirectory() as folder:
            client=FixedNativeClient();bridge=TauBridge(client,folder,arm='direct',cny_limit=1.)
            with self.assertRaises(RuntimeError):bridge.generate('fixed',[UserMessage(role='user',content='x')],call_name='agent_response')
            self.assertTrue(bridge.budget_exhausted);self.assertEqual(client.calls,[])
    def test_actual_runtime_attempt_preserves_native_call_and_official_messages(self):
        from tau2.data_model.message import UserMessage
        with tempfile.TemporaryDirectory() as folder:
            client=FixedNativeClient()
            bridge=TauBridge(client,folder,cny_limit=10,max_calls=2,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            with patch.object(ModelGateway,'from_env',side_effect=lambda log:ModelGateway(client,log)):
                result=bridge.generate('fixed-native',[UserMessage(role='user',content='lookup 123')],call_name='agent_response')
            self.assertIsNone(result.content)
            self.assertEqual(result.tool_calls[0].name,'lookup')
            self.assertEqual(result.tool_calls[0].arguments,{'id':'123'})
            wire=json.loads(client.calls[0][0]['content'].split(':',1)[1])
            self.assertEqual(wire['messages'],[{'role':'user','content':'lookup 123'}])
            self.assertEqual(bridge.records[0]['status'],'succeeded')
            self.assertEqual(len(list(Path(folder).glob('*/*/events.jsonl'))),1)

    def test_invalid_native_arguments_fail_without_replaying(self):
        from tau2.data_model.message import UserMessage
        class Bad(FixedNativeClient):
            def complete(self,messages,max_tokens):
                r=super().complete(messages,max_tokens);body=json.loads(r['content'])
                body['message']['tool_calls'][0]['function']['arguments']='not json'
                r['content']=json.dumps(body);return r
        with tempfile.TemporaryDirectory() as folder:
            client=Bad();bridge=TauBridge(client,folder,arm='direct')
            with self.assertRaises(ValueError):bridge.generate('fixed',[UserMessage(role='user',content='x')],call_name='agent_response')
            self.assertEqual(len(client.calls),1);self.assertEqual(bridge.records[0]['status'],'failed')


if __name__=='__main__':unittest.main()
