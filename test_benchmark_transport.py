import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agent_runtime.benchmark_transport import encode,complete
from agent_runtime.model_gateway import DeepSeekClient,ModelError


class NativeTransportTests(unittest.TestCase):
    def test_tool_only_reply_and_request_protocol_preserved(self):
        history=[{'role':'user','content':'lookup'},
                 {'role':'assistant','content':None,'tool_calls':[{'id':'c1','type':'function','function':{'name':'lookup','arguments':'{}'}}]},
                 {'role':'tool','content':'result','tool_call_id':'c1'}]
        tools=[{'type':'function','function':{'name':'lookup','parameters':{'type':'object'}}}]
        answer={'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'c2','type':'function','function':{'name':'lookup','arguments':'{}'}}]},'finish_reason':'tool_calls'}],'usage':{'prompt_tokens':20,'completion_tokens':5}}
        captured=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return json.dumps(answer).encode()
        class Opener:
            def open(self,request,timeout):captured.append(json.loads(request.data));return Response()
        with patch('urllib.request.build_opener',return_value=Opener()):
            result=complete(DeepSeekClient('synthetic-test-key'),encode(history,tools),256)
        self.assertEqual(captured[0]['messages'],history);self.assertEqual(captured[0]['tools'],tools)
        self.assertEqual(json.loads(result['content'])['message'],answer['choices'][0]['message'])
        self.assertEqual(result['usage']['prompt_tokens'],20)

    def test_invalid_options_fail_before_provider(self):
        for kwargs in ({'temperature':float('nan')},{'seed':'x'},{'tool_choice':'shell'}):
            with self.assertRaises(ValueError):encode([{'role':'user','content':'x'}],**kwargs)

    def test_error_does_not_echo_provider_secret(self):
        with patch('urllib.request.build_opener',side_effect=RuntimeError('private-provider-detail')):
            with self.assertRaises(ModelError) as result:complete(DeepSeekClient('synthetic-test-key'),encode([{'role':'user','content':'x'}]),256)
        self.assertNotIn('private-provider-detail',str(result.exception))


if __name__=='__main__':unittest.main()
