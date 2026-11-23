"""Unit & fault-injection tests for Priorities 1-5 Runtime Optimizations."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from aios.utils.logger import SchedulerLogger, _SafeStreamHandler
from cerebrum.llm.apis import LLMQuery, LLMResponse
from cerebrum.tool.apis import ToolQuery
from runtime import agent_loop
from runtime.launch import AIOSKernelRuntime


class _FixedCloudStub:
    def complete(self, query):
        time.sleep(0.003)
        return LLMResponse(response_message='cloud-ok', finished=True, status_code=200, usage={'total_tokens': 32})


class _BrokenStream:
    def write(self, data):
        raise BrokenPipeError('simulated broken log pipe')

    def flush(self):
        raise BrokenPipeError('simulated broken log pipe')


class PriorityOptimizationsTest(unittest.TestCase):
    def test_p1_cloud_api_zero_idle_queue_wait(self):
        with TemporaryDirectory() as tmp, AIOSKernelRuntime(
            scheduler_policy='fifo', storage_root=str(Path(tmp) / 'store'), batch_interval=0.05, log_mode='silent'
        ) as kernel:
            kernel.llm.model_service = _FixedCloudStub()
            res = kernel.execute_request('p1_agent', LLMQuery(model='deepseek-chat', messages=[{'role': 'user', 'content': 'hi'}]))
            self.assertEqual(res['response'].status_code, 200)
            self.assertLess(res['waiting_times'][0], 0.02)

    def test_p2_task_scope_isolation_and_full_reclamation(self):
        with TemporaryDirectory() as tmp, AIOSKernelRuntime(
            storage_root=str(Path(tmp) / 'store'), log_mode='silent'
        ) as kernel:
            with kernel.task_scope('task_alpha', max_model_calls=5, max_tokens=2000) as scope:
                ctx_id = scope.put_context({'secret': 'alpha-only', 'goal': 'keep-safe'})
                self.assertIsNotNone(scope.ipc_token)
                with self.assertRaises(PermissionError):
                    kernel.context_store.get(ctx_id, reader_agent='task_beta')
                r = scope.execute(LLMQuery(model='mock-aios-core', messages=[{'role': 'user', 'content': 'hello'}]))
                self.assertEqual(r['response'].status_code, 200)

            # After scope exit: agent terminated, context reclaimed, DRF used == 0
            self.assertEqual(kernel.scheduler.drf_controller.snapshot()['agents']['task_alpha']['state'], 'terminated')
            self.assertEqual(kernel.scheduler.drf_controller.used.cpu_milli, 0)
            with self.assertRaises(KeyError):
                kernel.context_store.get(ctx_id, reader_agent='task_alpha')

    def test_p3_large_tool_output_offloaded_and_paginated_with_preserved_constraints(self):
        with TemporaryDirectory() as tmp, AIOSKernelRuntime(
            storage_root=str(Path(tmp) / 'store'), log_mode='silent'
        ) as kernel:
            big_text = 'CONSTRAINT_KEEP_ME ' + ('SEGMENT_DATA_0123456789 ' * 160) + 'TAIL_MARKER_END'
            seen_histories = []

            def fake_model(history, tools=None, return_response=True):
                seen_histories.append(history)
                turn = len(seen_histories)
                if turn == 1:
                    return LLMResponse(
                        response_message='',
                        tool_calls=[{'id': 'c1', 'function': {'name': 'file_write', 'arguments': json.dumps({'path': 'big.txt', 'content': big_text})}}],
                        status_code=200,
                    )
                if turn == 2:
                    return LLMResponse(
                        response_message='',
                        tool_calls=[{'id': 'c2', 'function': {'name': 'file_read', 'arguments': json.dumps({'path': 'big.txt'})}}],
                        status_code=200,
                    )
                if turn == 3:
                    tool_payload = json.loads(history[-1]['content'])
                    self.assertIn('context_ref', tool_payload)
                    self.assertTrue(tool_payload['has_more'])
                    return LLMResponse(
                        response_message='',
                        tool_calls=[{
                            'id': 'c3',
                            'function': {
                                'name': 'context_read_page',
                                'arguments': json.dumps({'ctx_id': tool_payload['context_ref'], 'offset': 1800, 'limit': 3000}),
                            },
                        }],
                        status_code=200,
                    )
                # Final turn: verify system constraint preserved and tail marker retrieved via pagination
                self.assertEqual(history[0]['role'], 'system')
                self.assertIn('MUST_KEEP_CONSTRAINT', history[0]['content'])
                self.assertIn('TAIL_MARKER_END', history[-1]['content'])
                return LLMResponse(response_message='verified-paginated', status_code=200)

            out = agent_loop.run(
                kernel,
                'p3_agent',
                [{'role': 'system', 'content': 'MUST_KEEP_CONSTRAINT'}, {'role': 'user', 'content': 'read big file'}],
                fake_model,
            )
            self.assertEqual(out['text'], 'verified-paginated')
            self.assertEqual(out['tool_failures'], [])

    def test_p4_local_validation_avoids_kernel_call_and_parallel_readonly(self):
        with TemporaryDirectory() as tmp, AIOSKernelRuntime(
            storage_root=str(Path(tmp) / 'store'), log_mode='silent'
        ) as kernel:
            before_syscalls = kernel.syscall_executor.id
            turns = []

            def fake_model(history, tools=None, return_response=True):
                turns.append(len(history))
                if len(turns) == 1:
                    # Invalid schema (missing required key + extra key): must be rejected locally with 0 kernel syscalls
                    return LLMResponse(
                        response_message='',
                        tool_calls=[{'id': 'bad1', 'function': {'name': 'math_calculate', 'arguments': json.dumps({'wrong': 123})}}],
                        status_code=200,
                    )
                if len(turns) == 2:
                    self.assertEqual(kernel.syscall_executor.id, before_syscalls)
                    # Parallel read-only batch (2 independent math calls)
                    return LLMResponse(
                        response_message='',
                        tool_calls=[
                            {'id': 'ro1', 'function': {'name': 'math_calculate', 'arguments': json.dumps({'expression': '2+3'})}},
                            {'id': 'ro2', 'function': {'name': 'math_calculate', 'arguments': json.dumps({'expression': '6*7'})}},
                        ],
                        status_code=200,
                    )
                self.assertIn('5', history[-2]['content'])
                self.assertIn('42', history[-1]['content'])
                return LLMResponse(response_message='done-p4', status_code=200)

            out = agent_loop.run(kernel, 'p4_agent', [{'role': 'user', 'content': 'calc'}], fake_model)
            self.assertEqual(out['text'], 'done-p4')
            self.assertEqual(out['tool_failures'], ['math_calculate'])

    def test_p5_broken_log_pipe_does_not_fail_task_execution(self):
        logger = SchedulerLogger('p5_broken_pipe_test', log_mode='console')
        broken_handler = _SafeStreamHandler(stream=_BrokenStream())
        logger._logger.handlers = [broken_handler]
        logger.log('this write hits broken pipe', 'info')
        self.assertGreaterEqual(broken_handler.dropped_logs, 1)

        with TemporaryDirectory() as tmp, AIOSKernelRuntime(
            storage_root=str(Path(tmp) / 'store'), log_mode='console'
        ) as kernel:
            kernel.scheduler.logger._logger.handlers = [broken_handler]
            res = kernel.execute_request('p5_agent', LLMQuery(model='mock-aios-core', messages=[{'role': 'user', 'content': 'resilient'}]))
            self.assertEqual(res['response'].status_code, 200)
            self.assertGreater(broken_handler.dropped_logs, 1)

    def test_boundary_shared_context_survives_first_scope_exit(self):
        with TemporaryDirectory() as tmp, AIOSKernelRuntime(
            storage_root=str(Path(tmp) / 'store'), log_mode='silent'
        ) as kernel:
            shared = {'goal': 'shared-dedup-doc', 'body': 'identical content ' * 40}
            with kernel.task_scope('scope_b') as sb:
                ctx_b = sb.put_context(shared)
                with kernel.task_scope('scope_a') as sa:
                    ctx_a = sa.put_context(shared)
                    self.assertEqual(ctx_a, ctx_b)
                # scope_a exited: scope_b MUST still be able to read the shared context; scope_a must not
                self.assertEqual(kernel.context_store.get(ctx_b, reader_agent='scope_b')['goal'], 'shared-dedup-doc')
                with self.assertRaises(PermissionError):
                    kernel.context_store.get(ctx_b, reader_agent='scope_a')
            # Now both scopes exited: physically reclaimed
            self.assertEqual(kernel.context_store.stats()['unique_objects'], 0)
            with self.assertRaises(KeyError):
                kernel.context_store.get(ctx_b, reader_agent='scope_b')

    def test_boundary_duplicate_active_scope_name_rejected(self):
        with TemporaryDirectory() as tmp, AIOSKernelRuntime(
            storage_root=str(Path(tmp) / 'store'), log_mode='silent'
        ) as kernel:
            with kernel.task_scope('dup_agent') as first:
                original_token = first.ipc_token
                with self.assertRaises(ValueError):
                    with kernel.task_scope('dup_agent'):
                        pass
                # First scope's token and state must remain untouched
                kernel.ipc_manager.verify_token('dup_agent', original_token)

    def test_boundary_sqlite_delete_failure_preserves_memory_and_disk(self):
        with TemporaryDirectory() as tmp, AIOSKernelRuntime(
            storage_root=str(Path(tmp) / 'store'), log_mode='silent'
        ) as kernel:
            ctx_id = kernel.context_store.put('owner1', {'important': 'keep-on-db-fail'})
            file_path = Path(kernel.context_store._meta[ctx_id]['file_path'])
            self.assertTrue(file_path.exists())
            with patch('aios.context.context_store.sqlite3.connect', side_effect=sqlite3.OperationalError('disk full')):
                with self.assertRaises(sqlite3.OperationalError):
                    kernel.context_store.delete(ctx_id, requester_agent='owner1')
            # Memory, disk file, and read access must all remain intact
            self.assertTrue(file_path.exists())
            self.assertEqual(kernel.context_store.get(ctx_id, reader_agent='owner1')['important'], 'keep-on-db-fail')

    def test_boundary_ipc_revoked_token_rejected_and_payload_immutable(self):
        with TemporaryDirectory() as tmp, AIOSKernelRuntime(
            storage_root=str(Path(tmp) / 'store'), log_mode='silent'
        ) as kernel:
            ipc = kernel.ipc_manager
            tok_s = ipc.issue_token('sender_a')
            tok_r = ipc.issue_token('recv_b')
            original_payload = {'items': [1, 2, 3]}
            sent = ipc.send('sender_a', 'recv_b', original_payload, request_id='imm1', token=tok_s)
            # Mutate caller dict and returned dict
            original_payload['items'].append(999)
            sent['payload']['items'].append(888)
            got = ipc.receive('recv_b', token=tok_r)
            self.assertEqual(got[0]['payload']['items'], [1, 2, 3])
            got[0]['payload']['items'].append(777)
            got_again = ipc.receive('recv_b', token=tok_r)
            self.assertEqual(got_again[0]['payload']['items'], [1, 2, 3])

            # Revoke sender token -> both old token and token=None must be rejected
            ipc.revoke_token('sender_a')
            with self.assertRaises(PermissionError):
                ipc.send('sender_a', 'recv_b', {'x': 1}, token=tok_s)
            with self.assertRaises(PermissionError):
                ipc.send('sender_a', 'recv_b', {'x': 1}, token=None)

    def test_boundary_cloud_pool_bounded_backlog_under_burst(self):
        class _SlowCloud:
            def complete(self, query):
                time.sleep(0.015)
                return LLMResponse(response_message='ok', finished=True, status_code=200, usage={'total_tokens': 10})

        with TemporaryDirectory() as tmp, AIOSKernelRuntime(
            scheduler_policy='fifo', storage_root=str(Path(tmp) / 'store'), log_mode='silent'
        ) as kernel:
            kernel.llm.model_service = _SlowCloud()
            kernel.scheduler.drf_controller.global_max_model_calls = 500
            max_qsize = 0

            def one(i):
                nonlocal max_qsize
                pool = kernel.scheduler._cloud_pool
                if pool is not None:
                    q = pool._work_queue.qsize()
                    if q > max_qsize:
                        max_qsize = q
                r = kernel.execute_request(f'burst_{i % 16}', LLMQuery(model='deepseek-chat', messages=[{'role': 'user', 'content': 'hi'}]))
                return r['response'].status_code

            with ThreadPoolExecutor(max_workers=40) as callers:
                codes = list(callers.map(one, range(80)))
            self.assertTrue(all(c == 200 for c in codes))
            # Bounded semaphore ensures executor internal work queue never piles up unboundedly
            self.assertLessEqual(max_qsize, kernel.scheduler._cloud_concurrency)
