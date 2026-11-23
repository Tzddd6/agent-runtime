import base64
import importlib.util
import io
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from cerebrum.llm.apis import LLMQuery
from cerebrum.memory.apis import MemoryQuery
from cerebrum.tool.apis import ToolQuery
from runtime.launch import AIOSKernelRuntime
from runtime.web import NativeConsole


def _has_pdf_extractor() -> bool:
    return bool(shutil.which('pdftotext') or importlib.util.find_spec('pymupdf') or importlib.util.find_spec('fitz'))


class IsolationAndUploadTest(unittest.TestCase):
    @unittest.skipUnless(_has_pdf_extractor(), 'PDF extraction requires pdftotext or pymupdf')
    def test_pdf_upload_with_real_text_extractor(self):
        # A minimal PDF fixture with explicit object offsets, generated without extra libraries.
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
                   b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
                   b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
                   b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
        content = b'BT /F1 12 Tf 50 700 Td (PDF fixture text) Tj ET'
        objects.append(b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream')
        raw, offsets = b'%PDF-1.4\n', [0]
        for index, body in enumerate(objects, 1):
            offsets.append(len(raw))
            raw += str(index).encode() + b' 0 obj\n' + body + b'\nendobj\n'
        position = len(raw)
        raw += b'xref\n0 6\n0000000000 65535 f \n' + b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
        raw += b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + str(position).encode() + b'\n%%EOF\n'
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            try:
                row = console.attachments.upload({'conversation_id': 'owner1', 'name': 'fixture.pdf', 'data': base64.b64encode(raw).decode()})
                self.assertIn('PDF fixture text', console.attachments.context([row['id']], 'owner1')['attachments'][0]['text_excerpt'])
            finally:
                console.close()

    def test_private_shared_and_cross_user_memory(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            private = kernel.execute_request('alice', MemoryQuery(operation_type='add_memory', user_id='one', params={'content': 'private quantum context'}))['response'].memory_id
            other = MemoryQuery(operation_type='get_memory', user_id='one', params={'id': private}, agent_id='alice')
            self.assertEqual(kernel.execute_request('bob', other)['response'].status_code, 403)
            self.assertEqual(kernel.execute_request('alice', MemoryQuery(operation_type='get_memory', user_id='two', params={'id': private}))['response'].status_code, 403)
            query = LLMQuery(model='mock-aios-core', user_id='one', messages=[{'role': 'user', 'content': 'quantum context'}])
            self.assertNotIn('[ctx:', kernel.execute_request('bob', query)['response'].response_message)
            kernel.execute_request('alice', MemoryQuery(operation_type='add_memory', user_id='one', params={
                'content': 'shared quantum context', 'metadata': {'policy': 'shared'}}))
            self.assertIn('[ctx:', kernel.execute_request('bob', query)['response'].response_message)

    def test_worker_timeout_then_success_and_exact_resource_release(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            query = ToolQuery(tool_calls=[{'name': 'runtime.load', 'parameters': {'seconds': 1}}], execution_timeout=0.05)
            self.assertGreaterEqual(kernel.execute_request('deadline', query)['response'].status_code, 400)
            self.assertEqual(kernel.process_service.snapshot()['active'], 0)
            self.assertEqual(kernel.scheduler.drf_controller.used.cpu_milli, 0)
            good = ToolQuery(tool_calls=[{'name': 'text.stats', 'parameters': {'text': 'one two'}}])
            self.assertEqual(kernel.execute_request('deadline', good)['response'].response_message['words'], 2)

    def test_docx_image_and_attachment_owner_checks(self):
        with TemporaryDirectory() as folder, AIOSKernelRuntime(storage_root=str(Path(folder) / 'store')) as kernel:
            console = NativeConsole(kernel, Path(folder) / 'jobs')
            try:
                store = console.attachments
                buffer = io.BytesIO()
                with ZipFile(buffer, 'w') as archive:
                    archive.writestr('[Content_Types].xml', '<Types/>')
                    archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>DOCX fixture</w:t></w:r></w:p></w:body></w:document>')
                result = store.upload({'conversation_id': 'owner1', 'name': 'fixture.docx', 'data': base64.b64encode(buffer.getvalue()).decode()})
                self.assertIn('DOCX fixture', store.context([result['id']], 'owner1')['attachments'][0]['text_excerpt'])
                with self.assertRaises((ValueError, KeyError)):
                    store.read(result['id'], 'owner2')
                with self.assertRaises(ValueError):
                    store.upload({'conversation_id': 'owner1', 'name': 'fake.png', 'data': base64.b64encode(b'not an image').decode()})
                try:
                    from PIL import Image
                except ImportError:
                    return
                buffer = io.BytesIO()
                Image.new('RGB', (2, 2), 'white').save(buffer, format='PNG')
                image = store.upload({'conversation_id': 'owner1', 'name': 'fixture.png', 'data': base64.b64encode(buffer.getvalue()).decode()})
                self.assertEqual((image['width'], image['height']), (2, 2))
                self.assertEqual(image['media'], 'image/jpeg')
            finally:
                console.close()
