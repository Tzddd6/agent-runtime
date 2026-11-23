import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from aios.llm_core.streaming import encode_responses_input, parse_chat_completions_sse_stream
from cerebrum.llm.apis import LLMResponse
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole


class NativeToolProtocolTest(unittest.TestCase):
    def test_fragmented_tool_only_chat_completion(self):
        chunks = [
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call_1', 'function': {'name': 'math_', 'arguments': '{"expression"'}}]}}]},
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': 'calculate', 'arguments': ':"6*7"}'}}]}, 'finish_reason': 'tool_calls'}]},
        ]
        raw = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
        result = parse_chat_completions_sse_stream(io.BytesIO(raw.encode()))
        self.assertEqual(result['tool_calls'][0]['function'], {'name': 'math_calculate', 'arguments': '{"expression":"6*7"}'})

    def test_responses_preserves_image_and_function_history(self):
        messages = [
            {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAA='}}]},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call_1', 'function': {'name': 'math_calculate', 'arguments': '{"expression":"6*7"}'}}]},
            {'role': 'tool', 'tool_call_id': 'call_1', 'content': '42'},
        ]
        result = encode_responses_input(messages)
        self.assertEqual([item['type'] for item in result['input']], ['message', 'function_call', 'function_call_output'])
        self.assertEqual(result['input'][0]['content'][0]['type'], 'input_image')

    def test_auto_agent_uses_actual_worker_output(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            try:
                count = []
                def complete(query):
                    count.append(query)
                    if len(count) == 1:
                        return LLMResponse(tool_calls=[{'id': 'call_1', 'function': {'name': 'math_calculate', 'arguments': '{"expression":"6*7"}'}}])
                    self.assertEqual(json.loads(query.messages[-1]['content'])['result'], {'result': 42})
                    return LLMResponse(response_message='42')
                with patch.object(console.model_service, 'complete', side_effect=complete):
                    result = console.conversations.submit({'conversation_id': uuid4().hex, 'request_id': uuid4().hex, 'prompt': 'calculate 6*7', 'mode': 'auto'})
                self.assertEqual(result['answer'], '42')
                self.assertEqual(result['status'], 'succeeded')
                self.assertEqual(len(count), 2)
                self.assertEqual(kernel.process_service.snapshot()['receipts'][-1]['operation'], 'math.calculate')
            finally:
                console.close()
