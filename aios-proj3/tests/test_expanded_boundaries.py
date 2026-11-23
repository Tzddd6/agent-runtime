from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from unittest.mock import patch
from aios.ipc.mailbox import IPCManager
from aios.context.context_store import ContextStore


class ExpandedIPCBoundaries(unittest.TestCase):
    def test_revocation_rejects_old_and_missing_credentials(self):
        ipc=IPCManager(); token=ipc.issue_token('a');ipc.revoke_token('a')
        for supplied in (None,token,'forged'):
            with self.subTest(token_present=supplied is not None):
                with self.assertRaises(PermissionError):ipc.send('a','b',{},token=supplied)
                with self.assertRaises(PermissionError):ipc.receive('a',token=supplied)
                with self.assertRaises(PermissionError):ipc.ack('a','missing',token=supplied)
        replacement=ipc.issue_token('a')
        ipc.send('a','b',{},token=replacement)
        with self.assertRaises(PermissionError):ipc.send('a','b',{},token=token)

    def test_unknown_supplied_token_is_not_a_capability(self):
        with self.assertRaises(PermissionError):IPCManager().send('a','b',{},token='invented')

    def test_payload_snapshot_and_delivery_copy(self):
        ipc=IPCManager();payload={'items':[1]}
        first=ipc.send('a','b',payload,request_id='one')
        payload['items'].append(2);first['payload']['items'].append(3)
        received=ipc.receive('b')[0]
        self.assertEqual(received['payload'],{'items':[1]})
        received['payload']['items'].append(4)
        self.assertEqual(ipc.receive('b')[0]['payload'],{'items':[1]})
        self.assertEqual(ipc.send('a','b',{'items':[1]},request_id='one')['payload'],{'items':[1]})

    def test_failed_reference_batch_does_not_partially_grant(self):
        with TemporaryDirectory() as folder:
            store=ContextStore(folder);ipc=IPCManager(context_store=store)
            own=store.put('a',{'secret':'one'});other=store.put('other',{'secret':'two'})
            with self.assertRaises(PermissionError):ipc.send('a','b',{},context_refs=[own,other])
            with self.assertRaises(PermissionError):store.get(own,'b')
            self.assertEqual(ipc.receive('b'),[])

    def test_failed_duplicate_put_does_not_grant_access(self):
        with TemporaryDirectory() as folder:
            store=ContextStore(folder);data={'secret':'unchanged'}
            key=store.put('owner',data)
            with patch.object(store,'_persist_index',side_effect=OSError('synthetic disk failure')):
                with self.assertRaises(OSError):store.put('owner',data,authorized_agents=['reader'])
            with self.assertRaises(PermissionError):store.get(key,'reader')

    def test_failed_first_put_is_not_published_in_memory(self):
        with TemporaryDirectory() as folder:
            store=ContextStore(folder)
            with patch.object(store,'_persist_index',side_effect=OSError('synthetic disk failure')):
                with self.assertRaises(OSError):store.put('owner',{'secret':'new'})
            self.assertEqual(store.stats()['unique_objects'],0)
            self.assertEqual(len(store._meta),0)
