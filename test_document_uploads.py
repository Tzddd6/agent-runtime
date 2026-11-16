"""Real parsers and integrity tests; no model credentials or paid calls."""
import base64
import hashlib
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile
from agent_runtime.attachments import AttachmentStore
from agent_runtime.database import RuntimeDatabase


def docx(content='DOCX fixture: 星河预算 73 元。', extra=None):
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml','<Types/>')
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'+content+'</w:t></w:r></w:p></w:body></w:document>')
        for key,value in (extra or {}).items():archive.writestr(key,value)
    return data.getvalue()


def pdf():
    stream=b'BT /F1 18 Tf 45 750 Td (PDF canary: ORBIT-472 budget 95) Tj ET'
    bodies=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    data=b'%PDF-1.4\n';offsets=[0]
    for i,body in enumerate(bodies,1):
        offsets.append(len(data));data+=str(i).encode()+b' 0 obj\n'+body+b'\nendobj\n'
    start=len(data);data+=b'xref\n0 6\n0000000000 65535 f \n'
    data+=b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return data+b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(start).encode()+b'\n%%EOF'


@unittest.skipUnless(os.name=='posix' and shutil.which('bwrap'),'requires isolated Linux parser')
class DocumentUploadTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.root=Path(self.temporary.name)
        self.db=RuntimeDatabase(self.root/'metadata');self.store=AttachmentStore(self.root/'attachments',self.db)

    def tearDown(self):self.temporary.cleanup()

    def upload(self,name,data):
        return self.store.upload({'conversation_id':'a'*32,'name':name,'data':base64.b64encode(data).decode()})

    def test_docx_text_reaches_workspace_with_original_and_text_hashes(self):
        original=docx();value=self.upload('中文文档.docx',original)
        self.assertGreater(value['text_chars'],15)
        row,raw=self.store.read(value['id'],'a'*32);self.assertEqual(raw,original)
        self.assertEqual(row['sha256'],hashlib.sha256(original).hexdigest())
        context=self.store.context([value['id']],'a'*32,self.root/'workspace')
        self.assertIn('星河预算 73 元',context['attachments'][0]['text_excerpt'])
        self.assertTrue(context['attachments'][0]['path'].endswith('.txt'))
        with self.assertRaises(ValueError):self.store.read(value['id'],'b'*32)
        with self.db.connect() as connection:connection.execute("UPDATE attachment_text SET text='tampered'")
        with self.assertRaisesRegex(ValueError,'完整性'):self.store.read(value['id'],'a'*32)

    @unittest.skipUnless(shutil.which('pdftotext'),'requires poppler-utils')
    def test_pdf_uses_actual_parser_and_rejects_fake_or_encrypted_data(self):
        value=self.upload('paper.pdf',pdf());row,_=self.store.read(value['id'],'a'*32)
        self.assertIn('ORBIT-472',row['extracted_text'])
        for raw in (b'not PDF',b'%PDF-1.4 invalid'):
            with self.assertRaises(ValueError):self.upload('bad.pdf',raw)

    def test_docx_archive_entities_macros_and_credentials_are_rejected(self):
        for raw in (b'bad zip',docx(extra={'../escape':'no'}),docx(extra={'word/vbaProject.bin':'macro'}),
                    docx('sk-'+'X'*30)):
            with self.assertRaises(ValueError):self.upload('unsafe.docx',raw)
        bad=io.BytesIO()
        with zipfile.ZipFile(bad,'w') as archive:
            archive.writestr('[Content_Types].xml','<Types/>')
            archive.writestr('word/document.xml','<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><a>&x;</a>')
        with self.assertRaises(ValueError):self.upload('entities.docx',bad.getvalue())
        self.assertEqual(self.db.status()['attachments'],0)

    def test_large_docx_is_explicitly_truncated(self):
        value=self.upload('long.docx',docx('甲'*121000));self.assertEqual(value['text_chars'],120000)
        self.assertIn('截断',value['note'])
        context=self.store.context([value['id']],'a'*32,self.root/'workspace')
        self.assertTrue(context['attachments'][0]['excerpt_partial'])
        self.assertEqual(len(context['attachments'][0]['text_excerpt']),4000)
        self.assertEqual(len((self.root/'workspace'/context['attachments'][0]['path']).read_text()),120000)

    def test_webp_and_bmp_are_normalized_and_spoofed_files_rejected(self):
        from PIL import Image
        for extension,format in (('webp','WEBP'),('bmp','BMP'),('jpg','JPEG')):
            data=io.BytesIO();Image.new('RGB',(60,40),'blue').save(data,format=format)
            value=self.upload('image.'+extension,data.getvalue());self.assertEqual(value['media'],'image/jpeg')
        with self.assertRaises(ValueError):self.upload('spoof.png',docx())

    def test_missing_doc_decoder_has_actionable_error(self):
        if shutil.which('antiword'):self.skipTest('antiword installed')
        with self.assertRaisesRegex(ValueError,'另存为 DOCX'):self.upload('legacy.doc',b'legacy')


if __name__=='__main__':unittest.main()
