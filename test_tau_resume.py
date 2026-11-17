import unittest,importlib.util
from types import SimpleNamespace as S
from tools.resume_tau_evaluation import pending_indices,trajectory_digest
class ResumeTests(unittest.TestCase):
 def test_completed_tasks_are_never_replayed(self):
  tasks=[S(id='0'),S(id='1')];done=[S(trial=0,task_id='0'),S(trial=0,task_id='1'),S(trial=1,task_id='0')]
  self.assertEqual([(i,t.id) for i,t in pending_indices(tasks,done,2)],[(1,'1')])
 def test_corrupt_or_duplicate_checkpoint_rejected(self):
  tasks=[S(id='0')]
  for rows in ([S(trial=0,task_id='0')]*2,[S(trial=2,task_id='0')],[S(trial=0,task_id='foreign')]):
   with self.assertRaises(ValueError):pending_indices(tasks,rows,2)
 def test_prefix_hash_changes_if_any_result_changes(self):
  original=[{'task_id':'0','reward':0},{'task_id':'1','reward':1}]
  self.assertNotEqual(trajectory_digest(original),trajectory_digest([{'task_id':'0','reward':1},original[1]]))

 @unittest.skipUnless(importlib.util.find_spec('tau2'),'official tau2 fixture required')
 def test_official_failure_envelope_does_not_retry_provider_request(self):
  from tau2.runner.progress import run_with_retry
  from tau2.data_model.simulation import TerminationReason
  calls=[]
  def failed():calls.append(1);raise RuntimeError('synthetic upstream failure')
  result=run_with_retry(failed,S(id='fixture'),0,303,max_retries=0,console_display=False)
  self.assertEqual(calls,[1]);self.assertEqual(result.termination_reason,TerminationReason.INFRASTRUCTURE_ERROR);self.assertEqual(result.messages,[])
if __name__=='__main__':unittest.main()
