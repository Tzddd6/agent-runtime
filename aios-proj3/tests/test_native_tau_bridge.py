from copy import deepcopy
from tempfile import TemporaryDirectory
import unittest
from cerebrum.llm.apis import LLMResponse
from runtime.launch import AIOSKernelRuntime
from aios.benchmarks.tau_bridge import NativeTauTransport


class FixedProvider:
    def __init__(self):
        self.inputs = []
        self.fail = False
    def complete(self, query):
        self.inputs.append(deepcopy(query.to_dict()))
        if self.fail:
            raise RuntimeError('synthetic timeout')
        return LLMResponse(response_message='fixed answer', usage={'total_tokens': 17},
            tool_calls=[{'id': 'call_1', 'type': 'function', 'function': {
                'name': 'get_booking', 'arguments': '{"id":"b1"}'}}])


class NativeTauBridgeTests(unittest.TestCase):
    def run_arm(self, arm, actor='agent', fail=False, blocked=False):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=folder) as kernel:
            provider = FixedProvider(); provider.fail = fail
            reserved = []; settled = []
            def reserve(query, who):
                if blocked:
                    raise RuntimeError('budget exhausted')
                reserved.append(who); return 1
            bridge = NativeTauTransport(kernel, provider, arm=arm, model='fixed-provider',
                reserve=reserve, settle=lambda amount, usage: settled.append(usage))
            messages=[{'role':'system','content':'Official fixture policy'},
                {'role':'assistant','content':None,'tool_calls':[{'id':'prev',
                 'type':'function','function':{'name':'get_booking','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':'prev','content':'{"status":"open"}'},
                {'role':'user','content':'Please check booking'}]
            try:
                if fail or blocked:
                    with self.assertRaises(RuntimeError):
                        bridge.complete(messages, actor=actor, seed=300, tool_choice='auto')
                    result=None
                else:
                    result=bridge.complete(messages, actor=actor, seed=300, tool_choice='auto')
                return provider.inputs, bridge.records, result, reserved, settled, kernel.syscall_executor.get_history()
            finally:
                bridge.close()

    def test_same_provider_inputs_outputs_and_usage(self):
        direct=self.run_arm('direct'); native=self.run_arm('runtime')
        self.assertEqual(direct[0],native[0])
        self.assertEqual(direct[2].to_dict(),native[2].to_dict())
        self.assertEqual(len(native[5]),1)
        self.assertEqual(len(direct[5]),0)
        self.assertEqual(native[0][0]['seed'],300)
        self.assertEqual(native[0][0]['tool_choice'],'auto')
        self.assertEqual(native[0][0]['messages'][2]['tool_call_id'],'prev')

    def test_simulator_bypasses_agent_queue_but_uses_budget(self):
        values=self.run_arm('runtime',actor='simulator')
        self.assertEqual(values[3],['simulator'])
        self.assertEqual(len(values[5]),0)
        self.assertEqual(len(values[4]),1)

    def test_failed_turn_is_not_replayed(self):
        values=self.run_arm('runtime',fail=True)
        self.assertEqual(len(values[0]),1)
        self.assertEqual(values[1][0]['status'],'failed')
        self.assertEqual(values[4],[])

    def test_budget_blocks_before_provider(self):
        values=self.run_arm('runtime',blocked=True)
        self.assertEqual(values[0],[])
        self.assertEqual(values[1],[])


class NativeTauProviderProtocolTests(unittest.TestCase):
    def test_seed_and_tool_choice_reach_provider_without_prompt_changes(self):
        import io
        import json
        from unittest.mock import patch
        from cerebrum.llm.apis import LLMQuery
        from aios.llm_core.adapter import LLMAdapter
        class Reply(io.BytesIO):
            headers = {'Content-Type': 'application/json'}
        received = []
        def open_fixed(request, **kwargs):
            received.append(json.loads(request.data))
            return Reply(json.dumps({'choices':[{'finish_reason':'tool_calls',
                'message':{'content':None,'tool_calls':[{'id':'t1','type':'function',
                    'function':{'name':'get_booking','arguments':'{}'}}]}}],
                'usage':{'total_tokens':12}}).encode())
        query=LLMQuery(messages=[{'role':'user','content':'official fixture'}],
                       seed=300, tool_choice='auto')
        adapter=LLMAdapter(use_context_manager=False)
        with patch('aios.llm_core.adapter.urllib.request.urlopen', side_effect=open_fixed):
            result=adapter._call_openai_compatible({'backend':'deepseek','name':'fixed'},query)
        self.assertEqual(received[0]['messages'],query.messages)
        self.assertEqual(received[0]['seed'],300)
        self.assertEqual(received[0]['tool_choice'],'auto')
        self.assertEqual(result.tool_calls[0]['id'],'t1')

    def test_length_truncation_is_a_terminal_failure(self):
        import io
        import json
        from unittest.mock import patch
        from cerebrum.llm.apis import LLMQuery
        from aios.llm_core.adapter import LLMAdapter
        class Reply(io.BytesIO):
            headers={'Content-Type':'application/json'}
        reply=Reply(json.dumps({'choices':[{'finish_reason':'length',
                       'message':{'content':'partial'}}]}).encode())
        adapter=LLMAdapter(use_context_manager=False)
        with patch('aios.llm_core.adapter.urllib.request.urlopen', return_value=reply) as opened:
            result=adapter._call_openai_compatible({'backend':'deepseek','name':'fixed'},LLMQuery())
        self.assertEqual(result.status_code,502)
        self.assertTrue(result.finished)
        self.assertEqual(opened.call_count,1)
