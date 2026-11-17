import json,os,tempfile,time,unittest
from pathlib import Path
from agent_runtime.transport_trace import Trace
@unittest.skipUnless(hasattr(os,'geteuid'),'owner-only diagnostics')
class TraceTests(unittest.TestCase):
 def test_log_only_accepts_fixed_stages_and_numeric_values(self):
  with tempfile.TemporaryDirectory() as d:
   marker=Path(d)/'marker';log=Path(d)/'log';marker.write_text(json.dumps({'expires_at':time.time()+600}));marker.chmod(0o600)
   t=Trace(marker=marker,log=log);self.assertTrue(t.enabled);t.emit('headers',status=200,sse=True)
   with self.assertRaises(ValueError):t.emit('headers',status='private-fixture')
   with self.assertRaises(ValueError):t.emit('private-fixture')
   self.assertNotIn('private-fixture',log.read_text());self.assertEqual(json.loads(log.read_text())['status'],200)
 def test_expired_or_open_permissions_disable_trace(self):
  with tempfile.TemporaryDirectory() as d:
   marker=Path(d)/'marker';log=Path(d)/'log';marker.write_text(json.dumps({'expires_at':0}));marker.chmod(0o600)
   t=Trace(marker=marker,log=log);self.assertFalse(t.enabled);t.emit('request_start');self.assertFalse(log.exists())
   marker.write_text(json.dumps({'expires_at':time.time()+600}));marker.chmod(0o644);self.assertFalse(Trace(marker=marker,log=log).enabled)
if __name__=='__main__':unittest.main()
