import subprocess,unittest,os
from unittest.mock import patch
if os.name=='posix':
 from tools.benchmark_tau import frozen_source_commit
@unittest.skipUnless(os.name=='posix','Linux benchmark tool')
class SourceGuardTests(unittest.TestCase):
 def test_valid_frozen_source(self):
  with patch('subprocess.check_output',return_value='a'*40+'\n'),patch('subprocess.run'):
   self.assertEqual(frozen_source_commit('/fixture'),'a'*40)
 def test_unknown_or_unreadable_source_rejected(self):
  for result in ('unknown','b'*39):
   with patch('subprocess.check_output',return_value=result),self.assertRaises(ValueError):frozen_source_commit('/fixture')
  with patch('subprocess.check_output',side_effect=subprocess.CalledProcessError(128,'git')),self.assertRaises(ValueError):frozen_source_commit('/fixture')
 def test_dirty_runtime_rejected(self):
  with patch('subprocess.check_output',return_value='a'*40),patch('subprocess.run',side_effect=subprocess.CalledProcessError(1,'git')),self.assertRaises(ValueError):frozen_source_commit('/fixture')
if __name__=='__main__':unittest.main()
