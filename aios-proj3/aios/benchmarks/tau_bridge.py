"""Native AIOS transport for tau2. Official tools/evaluator remain upstream.

A paid driver must supply a shared reservation/settlement callback covering agent
and simulator. This module contains no credentials or implicit network client.
"""
from copy import deepcopy
import json
from time import perf_counter
from cerebrum.llm.apis import LLMQuery


class NativeTauTransport:
    def __init__(self, kernel, provider, *, arm, reserve, settle, model, record=None):
        if arm not in ('runtime', 'direct'):
            raise ValueError('Unknown comparison arm')
        if not callable(reserve) or not callable(settle):
            raise ValueError('Shared budget callbacks are required')
        self.kernel, self.provider, self.arm = kernel, provider, arm
        self.reserve, self.settle, self.model = reserve, settle, model
        self.records = []
        self.record = record or (lambda value: None)
        self._previous_service = getattr(kernel.llm, 'model_service', None)
        kernel.llm.model_service = provider

    def complete(self, messages, tools=None, *, actor='agent', max_tokens=2048,
                 temperature=0.0, seed=None, tool_choice=None):
        if actor not in ('agent', 'simulator', 'judge'):
            raise ValueError('Unknown evaluation actor')
        if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
            raise ValueError('Invalid evaluation output allowance')
        query = LLMQuery(messages=deepcopy(messages), tools=deepcopy(tools),
                         model=self.model, max_new_tokens=max_tokens,
                         temperature=temperature, seed=seed, tool_choice=tool_choice)
        # Fresh kernel with no benchmark memory: no cross-task recall or prompts.
        reservation = self.reserve(query, actor)
        start = perf_counter()
        metric = {'actor': actor, 'arm': self.arm, 'status': 'failed', 'calls': 1}
        try:
            if self.arm == 'runtime' and actor == 'agent':
                result = self.kernel.execute_request('tau-agent', query)
                response = result['response']
                metric['queue_wait_seconds'] = sum(result['waiting_times'])
                metric['syscall_id'] = result['syscall_id']
            else:
                response = self.provider.complete(query)
            if response.status_code != 200 or not response.finished or response.error:
                raise RuntimeError('Incomplete evaluation turn; no replay')
            # Settle actual usage before message validation; malformed outputs cost.
            self.settle(reservation, dict(response.usage))
            metric.update(status='succeeded', usage=dict(response.usage))
            return response
        finally:
            metric['seconds'] = perf_counter() - start
            self.records.append(metric)
            self.record(dict(metric))  # Metadata only, no messages/credentials.

    def generate(self, model, messages, tools=None, tool_choice=None, call_name=None, **kwargs):
        from tau2.utils.llm_utils import to_litellm_messages, validate_message_history
        from tau2.data_model.message import AssistantMessage, ToolCall
        if model != self.model:
            raise ValueError('Frozen model mismatch')
        unsupported = set(kwargs) - {'temperature', 'seed', 'max_tokens'}
        if unsupported:
            raise ValueError('Unsupported benchmark generation option')
        validate_message_history(messages)
        actor = 'simulator' if call_name == 'user_simulator_response' else 'agent' if call_name == 'agent_response' else 'judge'
        response = self.complete(to_litellm_messages(messages),
                                 [tool.openai_schema for tool in tools] if tools else None,
                                 actor=actor, tool_choice=tool_choice,
                                 max_tokens=kwargs.get('max_tokens', 2048),
                                 temperature=kwargs.get('temperature', 0.0), seed=kwargs.get('seed'))
        calls = []
        for call in response.tool_calls or []:
            function = call.get('function', {})
            arguments = function.get('arguments', call.get('parameters', {}))
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError('Invalid official tool arguments')
            calls.append(ToolCall(id=call['id'], name=function.get('name', call.get('name')),
                                  arguments=arguments))
        return AssistantMessage(role='assistant', content=response.response_message or None,
                                tool_calls=calls or None, usage=response.usage,
                                generation_time_seconds=self.records[-1]['seconds'])

    def install(self):
        import tau2.agent.llm_agent as agent
        import tau2.user.user_simulator as user
        self._originals = (agent.generate, user.generate)
        agent.generate = self.generate
        user.generate = self.generate

    def close(self):
        if hasattr(self, '_originals'):
            import tau2.agent.llm_agent as agent
            import tau2.user.user_simulator as user
            agent.generate, user.generate = self._originals
        if self._previous_service is None:
            del self.kernel.llm.model_service
        else:
            self.kernel.llm.model_service = self._previous_service
