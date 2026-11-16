import hashlib,json,os,signal,sys,tempfile,time,unittest
from unittest.mock import patch
from pathlib import Path
from agent_runtime.compact_patch import compile_patch,describe
from agent_runtime.context import ContextStore
from agent_runtime.ipc import Broker

PRICING_EDITS={'edits':[
 {'site':'line_total_cents.return.0','expression':'unit_cents * quantity * (100 - discount_pct) // 100'},
 {'site':'shipping_cents.if.1','expression':'subtotal_cents >= 5000'},
 {'site':'refund_cents.return.0','expression':'paid_cents * returned_qty // total_qty'}]}


class V18Tests(unittest.TestCase):
    def test_auto_backend_uses_no_preloader_for_single_leaf_but_reuses_dynamic_graph(self):
        from agent_runtime.runtime import run
        with tempfile.TemporaryDirectory() as folder:
            for index,tasks in enumerate(([{'id':'one'}],[{'id':'parent','spawn':[{'id':'child','depends_on':['parent']}]}])):
                path=Path(folder)/str(index)
                states,_=run(tasks,quiet=True,output_dir=path)
                self.assertTrue(all(s=='succeeded' for s in states.values()))
                summary=json.loads(next(path.glob('*/summary.json')).read_text())
                self.assertEqual(summary['execution_stats']['backend'],'spawn' if index==0 or sys.platform!='linux' else 'forkserver')

    def test_expression_patch_preserves_guards_and_rejects_unsafe_or_stale_sites(self):
        source=compile_patch(json.dumps(PRICING_EDITS),'pricing')
        self.assertIn('unit_cents < 0',source);self.assertNotIn('if False',source)
        for edits in ([{'site':'missing','expression':'1'}],[{'site':'line_total_cents.return.0','expression':'__import__("os").system("id")'}],[{'site':'line_total_cents.return.0','expression':'1 if True else 0'}]):
            with self.assertRaises(ValueError):compile_patch(json.dumps({'edits':edits}),'pricing')
        self.assertNotIn('source',describe('pricing'))

    def test_context_batch_checks_all_authority_and_enforces_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ContextStore(Path(tmp));a=store.put({'hello':'甲'},['a']);b=store.put('private',['b']);broker=Broker(store,['a','b'])
            try:
                token=broker.issue('a')
                result=broker.dispatch(token,{'op':'materialize','context_refs':[a,a],'input_refs':{'up':a}})
                self.assertEqual(result,{'context':[{'hello':'甲'},{'hello':'甲'}],'upstream_results':{'up':{'hello':'甲'}}})
                with self.assertRaises(PermissionError):broker.dispatch(token,{'op':'materialize','context_refs':[a],'input_refs':{'other':b}})
                with self.assertRaises(ValueError):store.get_many([a],'a',max_bytes=1)
                broker.revoke(token)
                with self.assertRaises(PermissionError):broker.dispatch(token,{'op':'materialize','context_refs':[]})
            finally:broker.close()

    def test_clock_is_actual_and_needs_no_paid_call(self):
        from agent_runtime.routing import auto_tasks
        from agent_runtime.clock_tool import now
        self.assertEqual(auto_tasks('现在几点了？')[0]['action'],'clock')
        result=now('Asia/Shanghai');self.assertLess(abs(result['unix_seconds']-time.time()),2)
        self.assertEqual(result['source'],'runtime_server_system_clock')
        with self.assertRaises(ValueError):now('../secret')

    @unittest.skipUnless(sys.platform=='linux','Linux prefork backend')
    def test_fork_children_have_separate_pids_and_fault_does_not_kill_peer(self):
        from agent_runtime.runtime import run
        tasks=[{'id':'bad','agent':'bad','action':'fail'}, {'id':'peer','agent':'peer','action':'echo','message':'still alive'},
               {'id':'next','agent':'peer','action':'echo','message':'fresh child','depends_on':['peer']}]
        with tempfile.TemporaryDirectory() as tmp:
            states,events=run(tasks,quiet=True,output_dir=tmp,worker_backend='forkserver',cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertEqual(states,{'bad':'failed','peer':'succeeded','next':'succeeded'})
            pids=[e['pid'] for e in events if e['state']=='running'];self.assertEqual(len(set(pids)),3)
            summary=json.loads(next(Path(tmp).glob('*/summary.json')).read_text())
            self.assertEqual(summary['execution_stats']['backend'],'forkserver');self.assertEqual(summary['execution_stats']['launches'],3)

    @unittest.skipUnless(sys.platform=='linux','Linux private preloader')
    def test_preloader_has_no_credentials_and_death_never_reports_success(self):
        from agent_runtime.executor import Executor
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'AGENTRT_PROXY_TOKEN':'fixture-must-not-enter-preloader'}):
            root=Path(tmp);executor=Executor(backend='forkserver',workflow_root=root)
            try:
                handle=executor.launch({'id':'slow','action':'sleep','seconds':3,'timeout':5},root/'slow',{'cpu_milli':1000,'memory_bytes':64*1024**2,'pids':8})
                preloader=executor.preloader.process
                self.assertNotIn(b'fixture-must-not-enter-preloader',Path(f'/proc/{preloader.pid}/environ').read_bytes())
                preloader.kill();preloader.wait(timeout=3)
                code,_,_=executor.cleanup(handle);self.assertNotEqual(code,0)
            finally:executor.close()

    @unittest.skipUnless(sys.platform=='linux','real protected compiler verification')
    def test_pricing_expression_patch_passes_actual_independent_cases(self):
        from agent_runtime.engineering import run_check
        with tempfile.TemporaryDirectory() as tmp:
            source=compile_patch(json.dumps(PRICING_EDITS),'pricing');report=run_check(source,Path(tmp)/'check','pricing')
            self.assertTrue(report['success']);self.assertEqual(report['heldout']['passed'],report['heldout']['total'])


if __name__=='__main__':unittest.main()
