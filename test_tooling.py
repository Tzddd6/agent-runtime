import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch
from agent_runtime.capabilities import execute_plugin
from agent_runtime.tooling import operate,public_address,command
from agent_runtime.runtime import run
from agent_runtime.model_gateway import ModelGateway
from agent_runtime.chat_store import answer_for


class ToolingTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='posix','safe file operations require Linux')
    def test_artifact_download_keeps_workspace_scope_and_hash(self):
        import time
        from agent_runtime.web import Controller
        with tempfile.TemporaryDirectory() as tmp:
            controller=Controller(tmp)
            tasks=[{'id':'write','action':'plugin','plugin':'artifact.write','arguments':{'path':'report.md','content':'verified artifact'}}]
            with patch('agent_runtime.scenarios.tasks_for',return_value=tasks):key=controller.submit({'preset':'document'})
            deadline=time.monotonic()+5
            while controller.detail(key)['status']=='running' and time.monotonic()<deadline:time.sleep(.02)
            event=next(e for e in controller.detail(key)['events'] if e['state']=='succeeded')
            digest=event['result']['result']['sha256']
            self.assertEqual(controller.project_file(key,'report.md',digest),b'verified artifact')
            with self.assertRaises(ValueError):controller.project_file(key,'../metadata/runtime.sqlite3')
            with self.assertRaises(ValueError):controller.project_file(key,'report.md','0'*64)

    @unittest.skipUnless(os.name=='posix','descriptor-relative file operations require Linux')
    def test_files_are_atomic_scoped_and_refuse_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp,tempfile.TemporaryDirectory() as outside:
            task={'_project_workspace':tmp}
            written=operate(task,'file.write',{'path':'src/app.py','content':'answer=42\n'})
            self.assertTrue(written['written']);self.assertEqual(operate(task,'file.read',{'path':'src/app.py'})['content'],'answer=42\n')
            for name in ('../secret','/etc/passwd','.env','x.pem','a/../../x'):
                with self.subTest(name=name),self.assertRaises(ValueError):operate(task,'file.read',{'path':name})
            (Path(tmp)/'link').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(ValueError):operate(task,'file.write',{'path':'link/secret.txt','content':'no'})
            self.assertFalse((Path(outside)/'secret.txt').exists())
            copied=operate(task,'file.copy',{'source':'src/app.py','target':'copy.py'})
            self.assertEqual(copied['sha256'],written['sha256'])
            changed=operate(task,'file.replace',{'path':'copy.py','old':'42','new':'43','sha256':copied['sha256']})
            self.assertNotEqual(changed['sha256'],copied['sha256'])
            with self.assertRaises(ValueError):operate(task,'file.replace',{'path':'copy.py','old':'43','new':'44','sha256':copied['sha256']})

    def test_data_tools_validate_instead_of_guessing(self):
        self.assertFalse(execute_plugin({'plugin':'json.validate','arguments':{'text':'{"x":NaN}'}})['valid'])
        result=execute_plugin({'plugin':'csv.profile','arguments':{'text':'a,b\n1,\n2,3,4'}})
        self.assertEqual((result['rows'],result['ragged_rows'],result['missing_cells']),(2,1,1))
        self.assertEqual(execute_plugin({'plugin':'text.search','arguments':{'text':'a.*\nabc','query':'.*'}})['matches'][0]['line'],1)

    def test_web_reader_blocks_all_private_and_mixed_dns_results(self):
        for addresses in (['127.0.0.1'],['169.254.169.254'],['10.0.0.1'],['8.8.8.8','127.0.0.1']):
            with self.subTest(addresses=addresses),patch('socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(a,443)) for a in addresses]):
                with self.assertRaises(ValueError):public_address('fixture.invalid')

    @unittest.skipUnless(os.name=='posix' and shutil.which('bwrap'),'requires rootless bubblewrap')
    def test_real_command_has_no_host_home_environment_or_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=command({'_project_workspace':tmp},{'argv':['python3','-c',
                'import os,socket;assert not os.path.exists("/home/tzd");assert "AGENTRT_PROXY_TOKEN" not in os.environ;print("ISOLATED")']})
            self.assertTrue(result['ok'],result);self.assertIn('ISOLATED',result['output'])
            self.assertFalse(result['network_enabled'])

    @unittest.skipUnless(os.name=='posix' and shutil.which('bwrap'),'requires rootless bubblewrap')
    def test_real_command_stops_excess_output_and_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            task={'_project_workspace':tmp}
            result=command(task,{'argv':['python3','-c','print("x"*200000)']})
            self.assertEqual(result['stop_reason'],'output_limit');self.assertLessEqual(len(result['output']),65536)
            result=command(task,{'argv':['python3','-c','import time;time.sleep(3)'],'timeout':.2})
            self.assertEqual(result['stop_reason'],'timeout');self.assertLess(result['seconds'],2)

    def test_real_runtime_observation_and_bounded_configuration(self):
        tasks=[{'id':'before','action':'plugin','plugin':'runtime.observe'},
            {'id':'configure','action':'plugin','plugin':'runtime.configure','arguments':{'workers':1,'policy':'fifo'},'depends_on':['before']},
            {'id':'after','action':'plugin','plugin':'runtime.observe','depends_on':['configure']}]
        with tempfile.TemporaryDirectory() as tmp:
            states,events=run(tasks,quiet=True,output_dir=tmp,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue(all(v=='succeeded' for v in states.values()),states)
            results={e['task_id']:e['result']['result'] for e in events if e['state']=='succeeded'}
            self.assertEqual(results['after']['settings'],{'workers':1,'policy':'fifo'})
            self.assertEqual(results['before']['budget'],results['after']['budget'])

    def test_reducing_parallelism_drains_existing_workers(self):
        tasks=[{'id':'slow','action':'sleep','seconds':.7,
                'resources':{'cpu_milli':250,'memory_bytes':64*1024**2,'pids':8}},
            {'id':'configure','action':'plugin','plugin':'runtime.configure','arguments':{'workers':1,'policy':'aging_fit'},
                'resources':{'cpu_milli':250,'memory_bytes':64*1024**2,'pids':8}},
            {'id':'after','action':'plugin','plugin':'runtime.observe','depends_on':['configure']}]
        with tempfile.TemporaryDirectory() as tmp:
            states,events=run(tasks,quiet=True,output_dir=tmp,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue(all(s=='succeeded' for s in states.values()),states)
            end=next(e['time'] for e in events if e['task_id']=='slow' and e['state']=='succeeded')
            begin=next(e['time'] for e in events if e['task_id']=='after' and e['state']=='running')
            self.assertGreaterEqual(begin,end)

    def test_agent_observes_tool_failure_and_changes_next_round(self):
        class Client:
            timeout=3;model='fixture';calls=0
            def complete(self,messages,max_tokens):
                self.calls+=1
                if self.calls==1:plan={'route':'collaborate','tasks':[{'id':'calc','plugin':'math.calculate','arguments':{'expression':'1/0'}}]}
                elif self.calls==2:
                    results=json.loads(messages[-1]['content'])['upstream_results']
                    assert results['auto_calc']['ok'] is False
                    plan={'route':'collaborate','reason':'change invalid expression','tasks':[{'id':'calc','plugin':'math.calculate','arguments':{'expression':'6*7'}}]}
                else:
                    assert json.loads(messages[-1]['content'])['upstream_results']['auto_r2_calc']['value']==42
                    contexts=json.loads(messages[-1]['content'])['context']
                    assert any(c.get('runtime_evidence') for c in contexts if isinstance(c,dict))
                    plan={'route':'direct','answer':'Verified 42 after correcting the input.'}
                return {'content':json.dumps(plan),'usage':{}}
        from agent_runtime.routing import auto_tasks
        client=Client()
        with tempfile.TemporaryDirectory() as tmp,patch.object(ModelGateway,'from_env',side_effect=lambda p:ModelGateway(client,p)):
            states,events=run(auto_tasks('fixture'),quiet=True,output_dir=tmp,cgroup_root=os.environ.get('AGENTRT_TEST_CGROUP'))
            self.assertTrue(all(v=='succeeded' for v in states.values()),states)
            self.assertEqual(client.calls,3);self.assertIn('auto_r2_calc',states)
            self.assertIn('Verified 42',answer_for({'preset':'auto','status':'succeeded','events':events}))

    @unittest.skipUnless(os.name=='posix' and shutil.which('bwrap'),'requires rootless bubblewrap')
    def test_same_size_fast_edit_invalidates_stale_python_bytecode(self):
        with tempfile.TemporaryDirectory() as tmp:
            task={'_project_workspace':tmp}
            result=operate(task,'file.write',{'path':'calc.py','content':'def add(a,b): return a-b\n'})
            compiled=command(task,{'argv':['python3','-c','import py_compile;py_compile.compile("calc.py");from calc import add;assert add(2,3)==-1']})
            self.assertTrue(compiled['ok'],compiled)
            operate(task,'file.replace',{'path':'calc.py','old':'a-b','new':'a+b','sha256':result['sha256']})
            self.assertFalse(list((Path(tmp)/'__pycache__').glob('calc.*.pyc')))
            checked=command(task,{'argv':['python3','-c','from calc import add;assert add(2,3)==5']})
            self.assertTrue(checked['ok'],checked)
