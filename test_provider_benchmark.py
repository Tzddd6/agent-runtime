"""Offline checks for budget guards and independent answer validation."""
import importlib.util,json,tempfile,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('provider_bench',Path(__file__).parent/'tools/benchmark_provider_paired.py');b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
class ProviderBenchmarkTests(unittest.TestCase):
 def test_grader_rejects_unsafe_or_wrong_repair(self):
  case=b.cases()[1]
  self.assertTrue(b.grade(case,json.dumps({'expression':'paid_cents * returned_qty // total_qty'})))
  for text in ['paid_cents*(returned_qty//total_qty)','__import__("os").system("id")','2 ** 999999999']:
   self.assertFalse(b.grade(case,json.dumps({'expression':text})))
  self.assertFalse(b.grade(b.cases()[0],json.dumps({'cpu_cores':.2,'memory_bytes':209715200,'swap_bytes':0,'reservation_equals_usage':0})))
 def test_durable_spend_reservation_and_no_failure_replay(self):
  class Failing:
   model=b.MODEL;timeout=1;calls=0
   def complete(self,*args):self.calls+=1;raise RuntimeError('private-response-sentinel')
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'ledger.json';ledger=b.Ledger(path);f=Failing();client=b.Measured(f,ledger)
   with self.assertRaises(RuntimeError):client.complete(b.message('small fixture'),128)
   self.assertEqual(f.calls,1);self.assertEqual(ledger.rows[0]['state'],'failed_billing_unknown')
   self.assertGreater(ledger.reserved,0);self.assertNotIn('private-response-sentinel',path.read_text())
   with self.assertRaises(ValueError):b.Ledger(path)
 def test_case_failure_is_recorded_and_next_case_runs_without_replay(self):
  with tempfile.TemporaryDirectory() as tmp:
   ledger=b.Ledger(Path(tmp)/'ledger.json');fixture=b.Fixture();client=b.Measured(fixture,ledger);attempts=[]
   def invoke(arm,messages):
    attempts.append(messages)
    if len(attempts)==1:
     row=ledger.admit(messages,256);row['state']='failed_billing_unknown';ledger.flush();raise RuntimeError('synthetic transport failure')
    return client.complete(messages,256)['content']
   failed=b.execute_case(b.cases()[0],'runtime',client,invoke,0)
   recovered=b.execute_case(b.cases()[1],'runtime',client,invoke,0)
   self.assertFalse(failed['passed']);self.assertEqual(failed['usage_unknown_calls'],1);self.assertTrue(recovered['passed']);self.assertEqual(len(attempts),2)
 def test_cleanup_failure_aborts_instead_of_starting_more_workers(self):
  with tempfile.TemporaryDirectory() as tmp:
   client=b.Measured(b.Fixture(),b.Ledger(Path(tmp)/'ledger.json'))
   def invoke(*args):raise RuntimeError('CLEANUP_FAILED: synthetic')
   with self.assertRaises(RuntimeError):b.execute_case(b.cases()[0],'runtime',client,invoke,0)
 def test_cost_guard_blocks_before_provider(self):
  with tempfile.TemporaryDirectory() as tmp:
   ledger=b.Ledger(Path(tmp)/'ledger.json',limit=.00000001)
   with self.assertRaises(ValueError):ledger.admit(b.message('large enough'),256)
   self.assertEqual(ledger.rows,[])
if __name__=='__main__':unittest.main()
