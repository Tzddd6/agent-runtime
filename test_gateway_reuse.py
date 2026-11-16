import tempfile,threading,unittest
from pathlib import Path
from agent_runtime.model_broker import BrokerClient
from agent_runtime.model_gateway import ModelGateway
class ReuseTests(unittest.TestCase):
 def test_closing_gateway_does_not_cancel_callers_shared_client(self):
  client=BrokerClient('offline-control-token');client.cancel_event=threading.Event()
  with tempfile.TemporaryDirectory() as folder:
   gateway=ModelGateway(client,Path(folder)/'log.jsonl');owned=gateway.client
   self.assertIsNot(owned,client);gateway.close()
   self.assertTrue(owned.cancel_event.is_set());self.assertFalse(client.cancel_event.is_set())
