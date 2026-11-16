"""AIOS access-matrix semantics adapted to workflow partitions; NOT native AIOS score.
Source: AIOS292d98f tests/modules/memory/test_context_injector_access_matrix.py.
"""
import tempfile,unittest
from pathlib import Path
from agent_runtime.context import ContextStore
class AccessMatrixTests(unittest.TestCase):
 def test_eight_partition_owner_sharing_combinations(self):
  for same_user in (False,True):
   for same_agent in (False,True):
    for shared in (False,True):
     with self.subTest(same_user=same_user,same_agent=same_agent,shared=shared),tempfile.TemporaryDirectory() as folder:
      a=ContextStore(Path(folder)/'partition-a');b=ContextStore(Path(folder)/'partition-b');ref=a.put({'private':'canary'},['owner'])
      if shared:a.grant(ref,'owner','peer')
      store=a if same_user else b;requester='owner' if same_agent else 'peer'
      if same_user and (same_agent or shared):self.assertEqual(store.get(ref,requester),{'private':'canary'})
      else:
       with self.assertRaises(PermissionError):store.get(ref,requester)
