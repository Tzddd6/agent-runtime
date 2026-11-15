import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from agent_runtime.database import RuntimeDatabase
from agent_runtime.attachments import AttachmentStore


class AttachmentDatabaseTests(unittest.TestCase):
    def test_private_database_scopes_attachments_and_stores_no_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=RuntimeDatabase(Path(tmp)/'db');store=AttachmentStore(Path(tmp)/'uploads',db)
            value=store.upload({'conversation_id':'a'*32,'name':'notes.txt','data':base64.b64encode(b'hello').decode()})
            row,raw=store.read(value['id'],'a'*32);self.assertEqual(raw,b'hello')
            with self.assertRaises(ValueError):store.read(value['id'],'b'*32)
            db.observe('fixture',{'reserved':{'cpu_milli':1},'sample_sequence':1});db.audit('fixture','file.read','succeeded')
            self.assertEqual(db.status(),{'engine':'sqlite','attachments':1,'observations':1,'audit_events':1})
            if os.name=='posix':self.assertEqual(db.path.stat().st_mode&0o777,0o600)
            with self.assertRaises(ValueError):store.upload({'conversation_id':'a'*32,'name':'secret.pem','data':base64.b64encode(b'private').decode()})

    def test_png_is_verified_normalized_and_has_no_metadata(self):
        try:from PIL import Image
        except ImportError:self.skipTest('Pillow not installed')
        with tempfile.TemporaryDirectory() as tmp:
            store=AttachmentStore(Path(tmp)/'uploads',RuntimeDatabase(Path(tmp)/'db'))
            buffer=io.BytesIO();Image.new('RGB',(32,24),'red').save(buffer,format='PNG')
            value=store.upload({'conversation_id':'a'*32,'name':'fixture.png','data':base64.b64encode(buffer.getvalue()).decode()})
            row,raw=store.read(value['id'],'a'*32)
            self.assertEqual(row['media'],'image/jpeg');self.assertEqual((row['width'],row['height']),(32,24))
            self.assertTrue(raw.startswith(b'\xff\xd8\xff'))
            with self.assertRaises(ValueError):store.upload({'conversation_id':'a'*32,'name':'bad.png','data':base64.b64encode(b'not an image').decode()})

    @unittest.skipUnless(os.name=='posix','safe file materialization requires Linux')
    def test_attachment_materializes_only_in_project_with_bounded_excerpt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=AttachmentStore(Path(tmp)/'uploads',RuntimeDatabase(Path(tmp)/'db'))
            value=store.upload({'conversation_id':'a'*32,'name':'data.csv','data':base64.b64encode(b'a,b\n1,2').decode()})
            context=store.context([value['id']],'a'*32,Path(tmp)/'workspace')
            self.assertTrue((Path(tmp)/'workspace'/context['attachments'][0]['path']).is_file())
            self.assertEqual(context['_images'],[])
