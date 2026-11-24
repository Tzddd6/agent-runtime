from pathlib import Path
from zipfile import ZipFile
from PIL import Image
import sys
root=Path(sys.argv[1]);root.mkdir(parents=True,exist_ok=True)
Image.new('RGB',(20,20),'white').save(root/'valid.png')
objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
content=b'BT /F1 12 Tf 50 700 Td (PDF acceptance fixture) Tj ET';objects.append(b'<< /Length '+str(len(content)).encode()+b' >>\nstream\n'+content+b'\nendstream')
raw=b'%PDF-1.4\n';offsets=[0]
for i,body in enumerate(objects,1):offsets.append(len(raw));raw+=str(i).encode()+b' 0 obj\n'+body+b'\nendobj\n'
pos=len(raw);raw+=b'xref\n0 6\n0000000000 65535 f \n'+b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets[1:]);raw+=b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(pos).encode()+b'\n%%EOF\n';(root/'valid.pdf').write_bytes(raw)
with ZipFile(root/'valid.docx','w') as z:
 z.writestr('[Content_Types].xml','<Types/>')
 z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>DOCX acceptance fixture</w:t></w:r></w:p></w:body></w:document>')

(root/'fixture.txt').write_text('Text acceptance fixture',encoding='utf-8')
