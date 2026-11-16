import hashlib,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from agent_runtime.computer_worker import perform
from agent_runtime.computer_network import resource,LAB_URL
from agent_runtime.routing import auto_tasks
from agent_runtime.capabilities import catalog
from agent_runtime.web import Controller

class ComputerTests(unittest.TestCase):
    def test_registered_and_obvious_browser_intent_has_no_paid_router(self):
        self.assertIn('computer.run',[x['id'] for x in catalog()['plugins']])
        with patch('agent_runtime.computer.available',return_value=True):
            work=auto_tasks('你上b站看看')
        self.assertEqual(len(work),1);self.assertEqual(work[0]['plugin'],'computer.run');self.assertEqual(work[0]['arguments']['url'],'https://www.bilibili.com/')
        with patch('agent_runtime.computer.available',return_value=True):
            offline=auto_tasks('不要联网，看看example.com')
        self.assertTrue(all(t.get('plugin') not in ('computer.run','web.fetch') for t in offline))
    def test_no_host_paths_or_arbitrary_code_and_stale_ref_is_rejected(self):
        for action in [{'action':'open','url':'file:///etc/passwd'},{'action':'evaluate','code':'secret'}, {'action':'press','key':'Control+L'}, {'action':'click','snapshot':'old','ref':'e1'}]:
            with self.subTest(action=action),self.assertRaises(ValueError):perform(None,action,{'id':'current'},{})
    def test_browser_network_has_only_one_explicit_local_fixture(self):
        self.assertTrue(resource(LAB_URL)['fixture'])
        for address in ['file:///etc/shadow','http://127.0.0.1/','https://name:password@example.com/','https://127.0.0.1/','https://169.254.169.254/']:
            with self.subTest(address=address),self.assertRaises(ValueError):resource(address)
    def test_image_download_requires_matching_hash_and_denies_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            controller=Controller(folder);key='a'*32;controller.jobs[key]={'workspace_key':key}
            scope=Path(folder)/'workspaces'/key;target=scope/'computer'/('b'*32)/'latest.jpg';target.parent.mkdir(parents=True)
            raw=b'\xff\xd8\xffsynthetic-image-fixture';target.write_bytes(raw);sha=hashlib.sha256(raw).hexdigest();name=target.relative_to(scope).as_posix()
            self.assertEqual(controller.project_file(key,name,sha),raw)
            with self.assertRaises(ValueError):controller.project_file(key,name,'0'*64)
            target.unlink()
            if os.name=='posix':
                target.symlink_to('/etc/passwd')
                with self.assertRaises(ValueError):controller.project_file(key,name,sha)
