"""Paired framework experiment: same prompts, provider and OS Worker checker.

Direct orchestration bypasses Kernel/Syscall/DAG; it retains the shared low-level
Worker sandbox to keep tool semantics and CPU/memory limits equal. Not a model
leaderboard and not an unprotected one-shot LLM baseline.
"""
import argparse
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import statistics
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aios.tool import order_project as order
from aios.os_kernel.process_service import ProcessService
from aios.scheduler.drf_admission import DRFAdmissionController
from cerebrum.llm.apis import LLMQuery, LLMResponse
from runtime.engineering_workflow import patch_messages, decode_patch
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole


class Provider:
    def __init__(self, client=None, budget=None):
        self.client, self.budget = client, budget
        self.records = []

    def complete(self, query):
        record = {'input_sha256': sha256(json.dumps(query.messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                  'max_tokens': query.max_new_tokens, 'temperature': query.temperature,
                  'status': 'failed', 'usage': {}, 'cost_cny': None}
        self.records.append(record)
        if self.client is None:
            # Test-only response, explicitly labelled synthetic in the report.
            from tests.test_order_project import fixed_files
            response = LLMResponse(response_message=json.dumps({'files': fixed_files()}))
            record.update(status='succeeded', synthetic=True)
            return response
        reservation = self.budget.reserve(query.max_new_tokens)
        start = time.perf_counter()
        try:
            from agent_runtime.benchmark_transport import encode
            wire = encode(query.messages, query.tools, temperature=query.temperature,
                          seed=query.seed, tool_choice=query.tool_choice)
            result = self.client.complete(wire, query.max_new_tokens)
            usage = result.get('usage') or {}
            record['usage'] = usage
            record['cost_cny'] = self.budget.settle(reservation, usage)
            if result.get('finish_reason') != 'stop':
                raise RuntimeError('Incomplete provider response; no automatic replay')
            message = json.loads(result['content'])['message']
            record['response_text'] = message.get('content') or ''
            record.update(status='succeeded', seconds=time.perf_counter()-start)
            return LLMResponse(response_message=message.get('content') or '', usage=usage)
        except Exception as exc:
            # Unknown calls retain the conservative reservation in the ledger.
            record.update(error_type=type(exc).__name__, seconds=time.perf_counter()-start)
            raise


def direct(provider, folder, group):
    service = ProcessService(Path(folder)/'workers', cgroup_root=group)
    service.admission = DRFAdmissionController()
    try:
        def check(files):
            return service.run('direct-order', 'project.workspace_check', {'files': files},
                               resources={'cpu_milli': 500, 'memory_bytes': 128*1024**2, 'pids': 16}, timeout=10)
        previous = check(dict(order.SOURCE))
        if previous['success']: raise RuntimeError('Baseline unexpectedly passed')
        for _ in range(2):
            response = provider.complete(LLMQuery(messages=patch_messages(previous['files'], previous['test_log']),
                                                  max_new_tokens=4096, temperature=0.0, stream=True))
            files = decode_patch(response.response_message)
            previous = check(files)
            if previous['success']: break
        if not previous['success']: raise RuntimeError('Independent tests failed')
        buffer = io.BytesIO()
        with ZipFile(buffer, 'w', ZIP_DEFLATED) as archive:
            for name, text in sorted({**files, 'order_app/__init__.py': '', 'tests/__init__.py': '',
                                     'tests/test_orders.py': order.TEST_SOURCE}.items()):
                archive.writestr(name, text)
        with ZipFile(io.BytesIO(buffer.getvalue())) as archive:
            readback = {name: archive.read(name).decode() for name in files}
        assert order.digest(readback) == previous['source_sha256']
        return previous, service.snapshot()['receipts']
    finally: service.close()


def setup_delegate():
    relative = Path('/proc/self/cgroup').read_text().split('0::', 1)[1].strip()
    root = Path('/sys/fs/cgroup')/relative.lstrip('/')
    control=root/'control';control.mkdir();(control/'cgroup.procs').write_text(str(os.getpid()))
    (root/'cgroup.subtree_control').write_text('+cpu +memory +pids')
    attempts=root/'attempts';attempts.mkdir();(attempts/'cgroup.subtree_control').write_text('+cpu +memory +pids')
    return str(attempts)


def main():
    p=argparse.ArgumentParser();p.add_argument('--output', type=Path, required=True)
    p.add_argument('--pairs', type=int, default=3);p.add_argument('--live', action='store_true')
    p.add_argument('--legacy-root');p.add_argument('--control-config', type=Path);p.add_argument('--ledger', type=Path)
    p.add_argument('--delegate', action='store_true');args=p.parse_args()
    if not 1<=args.pairs<=10: raise ValueError('pairs must be 1..10')
    args.output.mkdir(parents=True,exist_ok=True)
    group=setup_delegate() if args.delegate else None
    client=budget=None
    if args.live:
        if args.pairs>2: raise ValueError('Live smoke is limited to two pairs')
        sys.path.insert(1,args.legacy_root)
        from agent_runtime.model_broker import BrokerClient
        from agent_runtime.tau_adapter import EvaluationBudget
        env=dict(line.split('=',1) for line in args.control_config.read_text().splitlines() if line.startswith('AGENTRT_PROXY_TOKEN='))
        client=BrokerClient(env['AGENTRT_PROXY_TOKEN'],'http://127.0.0.1:18888')
        profiles=[x for x in client.profiles_info()['profiles'] if x.get('configured') and x.get('provider')=='deepseek' and x.get('model')=='deepseek-flash']
        if len(profiles)!=1: raise RuntimeError('Exactly one configured DeepSeek Flash profile required')
        client=client.for_profile(profiles[0]['id'],'deepseek-flash')
        budget=EvaluationBudget(20,10000,args.ledger)
        with budget.transaction(): before=budget.spent
    else: before=0
    rows=[]
    for pair in range(args.pairs):
        for arm in (('runtime','direct') if pair%2==0 else ('direct','runtime')):
            provider=Provider(client,budget)
            row={'pair':pair,'arm':arm,'status':'failed'}
            started=time.perf_counter();cpu=time.process_time()
            try:
                with TemporaryDirectory() as folder:
                    if arm=='runtime':
                        with AIOSKernelRuntime(storage_root=str(Path(folder)/'store'),cgroup_root=group) as kernel:
                            console=NativeConsole(kernel,Path(folder)/'jobs');console.model_service.complete=provider.complete
                            row['initialization_seconds'] = time.perf_counter()-started
                            try:
                                result=console.conversations.submit({'conversation_id':uuid4().hex,'request_id':uuid4().hex,
                                    'mode':'engineering','project':'order_project','prompt':'Repair order project', 'use_memory':False,'use_knowledge':False})
                                if result['status']!='succeeded':
                                    row['failure_workflow'] = console.detail(result['job_id'])
                                    raise RuntimeError('Runtime workflow failed')
                                output=json.loads(result['answer']);receipts=kernel.process_service.snapshot()['receipts']
                            finally:console.close()
                    else: output,receipts=direct(provider,folder,group)
                    row.update(status='succeeded',tests_run=output['tests_run'],source_sha256=output['source_sha256'],
                               worker_receipts=receipts)
            except Exception as exc: row['error_type']=type(exc).__name__
            row.update(seconds=time.perf_counter()-started,coordinator_cpu_seconds=time.process_time()-cpu,
                       provider_calls=provider.records)
            rows.append(row)
            (args.output/f'{pair}-{arm}.json').write_text(json.dumps(row,indent=2))
            print(json.dumps({k:row[k] for k in ('pair','arm','status','seconds')}),flush=True)
            if args.live and any(r['status']=='failed' for r in provider.records):
                break
        if args.live and any(r['status']=='failed' for r in rows): break
    summary={}
    for arm in ('runtime','direct'):
        chosen=[r for r in rows if r['arm']==arm]
        summary[arm]={'runs':len(chosen),'successes':sum(r['status']=='succeeded' for r in chosen),
                      'median_seconds':statistics.median(r['seconds'] for r in chosen) if chosen else None,
                      'calls':sum(len(r['provider_calls']) for r in chosen),
                      'prompt_tokens':sum(c['usage'].get('prompt_tokens',0) for r in chosen for c in r['provider_calls']),
                      'completion_tokens':sum(c['usage'].get('completion_tokens',0) for r in chosen for c in r['provider_calls'])}
    pairs_equal=[]
    for pair in range(args.pairs):
        rs=[r for r in rows if r['pair']==pair]
        pairs_equal.append(len(rs)==2 and [c['input_sha256'] for c in rs[0]['provider_calls']]==[c['input_sha256'] for c in rs[1]['provider_calls']])
    if budget:
        with budget.transaction(): after=budget.spent
    else: after=0
    report={'scope':'cold end-to-end; direct orchestration shares low-level OS Worker/checker, bypasses Kernel/Syscall/DAG',
            'provider':'deepseek-flash' if args.live else 'fixed synthetic; no tokenizer or billing claims',
            'same_prompt_hash_by_pair':pairs_equal,'summary':summary,'cost_cny':after-before,'budget_after_cny':after,
            'resource_boundary':'coordinator CPU and Worker cgroup receipts; no per-arm total RSS attribution',
            'rows':rows}
    (args.output/'summary.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({k:report[k] for k in ('provider','same_prompt_hash_by_pair','summary','cost_cny','budget_after_cny')}),flush=True)


if __name__=='__main__':main()
