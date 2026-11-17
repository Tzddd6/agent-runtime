from pathlib import Path
import tempfile
import unittest
from agent_runtime.context import ContextStore
from agent_runtime.ipc import Broker


class MessageAckTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.broker=Broker(ContextStore(Path(self.temp.name)),['a','b','receiver'])
        self.addCleanup(self.broker.close)
        self.a=self.broker.issue('a'); self.b=self.broker.issue('b')
        self.receiver=self.broker.issue('receiver')

    def test_same_body_and_request_id_from_different_senders_have_distinct_ack_identity(self):
        request={'op':'send','to':'receiver','request_id':'r1','data':{'same':'body'}}
        first=self.broker.dispatch(self.a,request)
        second=self.broker.dispatch(self.b,request)
        self.assertEqual(first['ref'],second['ref'])
        self.assertNotEqual(first['message_id'],second['message_id'])
        self.assertEqual(first,self.broker.dispatch(self.a,request))
        ack={'op':'ack','message_id':first['message_id']}
        self.broker.dispatch(self.receiver,ack)
        self.broker.dispatch(self.receiver,ack)
        self.assertEqual(self.broker.dispatch(self.receiver,{'op':'receive'})['message'],second)

    def test_ambiguous_legacy_ack_is_rejected_without_dequeue(self):
        receipt=self.broker.dispatch(self.a,{'op':'send','to':'receiver','request_id':'r','data':'hello'})
        with self.assertRaises(ValueError):
            self.broker.dispatch(self.receiver,{'op':'ack','ref':receipt['ref'],'request_id':'r'})
        self.assertEqual(self.broker.dispatch(self.receiver,{'op':'receive'})['message'],receipt)


if __name__=='__main__':unittest.main()
