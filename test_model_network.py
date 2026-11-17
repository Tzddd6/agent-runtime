import json,os,stat,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from agent_runtime.model_network import read_proxy,provider_opener,PROXY_URL

@unittest.skipUnless(hasattr(os,'geteuid'),'POSIX private egress config')
class NetworkTests(unittest.TestCase):
 def fixture(self,root,**changes):
  p=Path(root)/'network.json';v={'proxy_url':PROXY_URL,'expires_at':time.time()+600,'owner_id':'synthetic-owner-001'};v.update(changes);p.write_text(json.dumps(v));p.chmod(0o600);return p
 def test_absence_and_expiry_and_endpoint(self):
  with tempfile.TemporaryDirectory() as d:
   self.assertIsNone(read_proxy(Path(d)/'missing'))
   self.assertEqual(read_proxy(self.fixture(d)),PROXY_URL)
   for change in ({'expires_at':0},{'expires_at':time.time()+7200},{'proxy_url':'http://example.com:1234'},{'owner_id':3}):
    with self.assertRaises(ValueError):read_proxy(self.fixture(d,**change))
 def test_reject_permissions_directory_and_symlink(self):
  with tempfile.TemporaryDirectory() as d:
   p=self.fixture(d);p.chmod(0o644)
   with self.assertRaises(ValueError):read_proxy(p)
   p.chmod(0o600);Path(d).chmod(0o755)
   with self.assertRaises(ValueError):read_proxy(p)
   Path(d).chmod(0o700);q=Path(d)/'link';q.symlink_to(p)
   with self.assertRaises(ValueError):read_proxy(q)
 def test_only_official_openai_uses_explicit_proxy(self):
  class Client:provider='openai';base='https://api.openai.com/v1';local_test=False
  c=Client()
  with patch('agent_runtime.model_network.read_proxy',return_value=PROXY_URL) as read,patch('urllib.request.build_opener') as build:
   provider_opener(c);self.assertEqual(read.call_count,1)
   self.assertEqual(build.call_args.args[0].proxies,{'https':PROXY_URL})
   c.provider='deepseek';provider_opener(c);self.assertEqual(read.call_count,1)
   c.provider='openai';c.local_test=True;provider_opener(c);self.assertEqual(read.call_count,1)
if __name__=='__main__':unittest.main()
