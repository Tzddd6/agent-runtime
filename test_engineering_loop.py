import hashlib,json,os,shutil,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agent_runtime.routing import auto_tasks,compact_catalog
from agent_runtime.repair_loop import detect_controlled
from agent_runtime.engineering import manifest,BASELINE_SOURCE
from agent_runtime.capabilities import execute_plugin,catalog

FIXED=BASELINE_SOURCE.replace('unit_cents * quantity - discount_pct','unit_cents * quantity * (100 - discount_pct) // 100').replace('subtotal_cents > 5000','subtotal_cents >= 5000').replace('paid_cents // total_qty * returned_qty','paid_cents * returned_qty // total_qty')


class EngineeringLoopTests(unittest.TestCase):
    def test_benchmark_runtime_child_actually_executes_with_offline_supplier(self):
        import io
        from tools.benchmark_engineering_loop import child
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'dataset.json').write_text(json.dumps({'cases':[{'group':'plain','prompt':'不需要工具，只输出JSON。','expected':{'value':5}}]}))
            with patch('sys.stdin',io.StringIO('READY\n')),patch.dict(os.environ,{'AGENTRT_MODEL_BROKER_URL':'http://127.0.0.1:18886','AGENTRT_PROXY_TOKEN':'offline-fixture-only'}),patch('agent_runtime.model_broker.BrokerClient.complete',return_value={'content':'{"value":5}','usage':{}}):
                child(tmp,0,'runtime',None)
            result=json.loads((root/'00-runtime-result.json').read_text())
            self.assertEqual(result['status'],'completed');self.assertEqual(len(result['calls']),1)
            self.assertEqual(json.loads(result['answer']),{'value':5})

    def test_local_profiles_preserve_intent_and_skip_paid_router(self):
        prompt='修复这个项目。'+json.dumps(manifest('pricing'),ensure_ascii=False)
        self.assertEqual(detect_controlled(prompt),'pricing')
        self.assertEqual(auto_tasks(prompt)[0]['action'],'repair_prepare')
        self.assertIsNone(detect_controlled('只分析，不要修改。'+json.dumps(manifest('pricing'))))
        self.assertEqual(auto_tasks('不需要工具，只输出JSON: 2+3')[0]['id'],'auto_answer')
        self.assertEqual(auto_tasks('不需要工具，执行命令检查')[0]['id'],'route')
        self.assertLess(len(compact_catalog('你好')),len(json.dumps(catalog(),ensure_ascii=False))/3)

    def test_library_sources_and_lazy_tool_discovery(self):
        result=execute_plugin({'plugin':'library.search','arguments':{'query':'unittest测试'}})
        self.assertTrue(result['sources']);self.assertIn('https://docs.python.org/3.11/library/unittest.html',result['sources'][0]['sources'])
        self.assertEqual(execute_plugin({'plugin':'tools.discover','arguments':{'query':'workspace.apply_patch'}})['tools'][0]['id'],'workspace.apply_patch')
        with self.assertRaises(ValueError):execute_plugin({'plugin':'library.read','arguments':{'id':'../secret'}})

    def test_curated_import_is_idempotent_and_preserves_user_changes(self):
        from agent_runtime.database import RuntimeDatabase
        from agent_runtime.knowledge import KnowledgeStore
        from tools.import_library import install
        with tempfile.TemporaryDirectory() as tmp:
            store=KnowledgeStore(RuntimeDatabase(tmp))
            self.assertTrue(all(r['decision']=='imported' for r in install(store,True)))
            self.assertTrue(all(r['decision']=='reused' for r in install(store,True)))
            row=store.listing()['documents'][0];doc=store.read(row['id'])
            store.save({'id':row['id'],'title':doc['title'],'body':'团队自己修改的资料','expected_sha256':doc['sha256']})
            self.assertIn('preserved_user_version',[r['decision'] for r in install(store,True)])

    @unittest.skipUnless(os.name=='posix','real protected file tools require Linux')
    def test_batch_rejects_stale_hash_or_test_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'a.py').write_text('x=1\n');(root/'b.py').write_text('x=2\n');(root/'test_a.py').write_text('x=3\n')
            task={'_workspace':tmp,'_project_workspace':tmp,'plugin':'workspace.apply_patch'}
            one={'path':'a.py','sha256':hashlib.sha256(b'x=1\n').hexdigest(),'content':'x=9\n'}
            for other in ({'path':'b.py','sha256':'0'*64,'content':'x=8\n'},{'path':'test_a.py','sha256':hashlib.sha256(b'x=3\n').hexdigest(),'content':'x=8\n'}):
                with self.assertRaises(ValueError):execute_plugin({**task,'arguments':{'patches':[one,other]}})
                self.assertEqual((root/'a.py').read_text(),'x=1\n')

    @unittest.skipUnless(os.name=='posix','verified artifact uses Linux scoped files')
    def test_controlled_loop_uses_one_call_and_delivers_exact_tested_artifact(self):
        from agent_runtime.runtime import run
        from agent_runtime.model_gateway import ModelGateway
        class Client:
            model='offline';timeout=2;calls=0
            def complete(self,messages,*args):
                self.calls+=1;return {'content':json.dumps({'source':FIXED}),'usage':{}}
        client=Client()
        with tempfile.TemporaryDirectory() as tmp,patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda p:ModelGateway(client,p)):
            states,events=run(auto_tasks('修复pricing'),output_dir=tmp,quiet=True,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue(all(s=='succeeded' for s in states.values()),states);self.assertEqual(client.calls,1)
            result=next(e['result']['result'] for e in events if e['task_id']=='repair_check_1' and e['state']=='succeeded')
            self.assertTrue(result['accepted']);v=result['verification']
            workspace=next(Path(tmp).glob('*/workspace'))
            self.assertEqual(hashlib.sha256((workspace/v['path']).read_bytes()).hexdigest(),v['source_sha256'])

    @unittest.skipUnless(os.name=='posix' and shutil.which('bwrap'),'requires real Linux bubblewrap')
    def test_workspace_loop_corrects_once_without_modifying_tests(self):
        from agent_runtime.runtime import run
        from agent_runtime.model_gateway import ModelGateway
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);workspace=root/'project';workspace.mkdir();(workspace/'calc.py').write_text('def add(a,b): return a-b\n')
            test='import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n def test_add(self): self.assertEqual(add(3,2),5)\n'
            (workspace/'test_calc.py').write_text(test)
            class Client:
                model='offline';timeout=2;calls=0
                def complete(self,messages,*args):
                    self.calls+=1
                    content='def add(a,b): return a-b\n' if self.calls==1 else 'def add(a,b): return a+b\n'
                    return {'content':json.dumps({'patches':[{'path':'calc.py','sha256':hashlib.sha256((workspace/'calc.py').read_bytes()).hexdigest(),'content':content}]}),'usage':{}}
            client=Client()
            with patch('agent_runtime.runtime.ModelGateway.from_env',side_effect=lambda p:ModelGateway(client,p)):
                states,events=run(auto_tasks('修复这个Python工程',workspace=workspace),workspace=workspace,output_dir=root/'runs',quiet=True,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue(all(s=='succeeded' for s in states.values()),states);self.assertEqual(client.calls,2)
            final=next(e['result']['result'] for e in events if e['task_id']=='repair_check_2' and e['state']=='succeeded')
            self.assertTrue(final['accepted']);self.assertEqual((workspace/'test_calc.py').read_text(),test)

    @unittest.skipUnless(os.name=='posix' and shutil.which('bwrap'),'requires real Linux bubblewrap')
    def test_zero_tests_is_not_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp,'calc.py').write_text('x=1\n')
            result=execute_plugin({'plugin':'workspace.test','arguments':{},'_workspace':tmp})
            self.assertFalse(result['success']);self.assertEqual(result['test_cases'],0)
            Path(tmp,'test_skip.py').write_text('import unittest\n@unittest.skip("fixture")\nclass T(unittest.TestCase):\n def test_skip(self): pass\n')
            result=execute_plugin({'plugin':'workspace.test','arguments':{},'_workspace':tmp})
            self.assertFalse(result['success']);self.assertEqual(result['test_cases'],1)


if __name__=='__main__':unittest.main()
