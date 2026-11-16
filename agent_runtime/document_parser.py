"""Bounded document extraction. Run only inside document_upload's isolated child."""
import json
from pathlib import Path
import resource
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

MAX_CHARS = 120000


def docx_text(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if (len(entries) > 512 or sum(e.file_size for e in entries) > 32*1024**2
                or len({e.filename for e in entries}) != len(entries)
                or any(e.flag_bits & 1 for e in entries)
                or any(e.filename.startswith('/') or '..' in Path(e.filename).parts for e in entries)
                or any(e.filename.lower().endswith('vbaproject.bin') for e in entries)):
            raise ValueError('Word 压缩包异常、加密或包含宏')
        names = ['word/document.xml'] + sorted(e.filename for e in entries
            if e.filename.startswith(('word/header', 'word/footer')) and e.filename.endswith('.xml'))
        if '[Content_Types].xml' not in archive.namelist():
            raise ValueError('不是有效的 DOCX 文档')
        result = []; characters = 0
        for name in names:
            info = archive.getinfo(name)
            if info.file_size > 4*1024**2:
                raise ValueError('Word 正文超过解析上限')
            raw = archive.read(name)
            if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
                raise ValueError('Word 不允许 XML 外部实体')
            root = ET.fromstring(raw)
            ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            for paragraph in root.iter(ns+'p'):
                text = ''.join((node.text or '') if node.tag == ns+'t' else
                               '\t' if node.tag == ns+'tab' else '\n' if node.tag == ns+'br' else ''
                               for node in paragraph.iter())
                result.append(text)
                characters += len(text)+1
                if characters > MAX_CHARS:
                    return '\n'.join(result), True
        return '\n'.join(result), False


def extract(path, suffix):
    truncated = False
    if suffix == '.docx':
        text, truncated = docx_text(path)
        note = '提取 Word 正文、表格文字及页眉页脚；不执行宏，不读取外部链接。'
    elif suffix == '.pdf':
        if not Path(path).read_bytes().startswith(b'%PDF-'):
            raise ValueError('PDF 文件头无效')
        output = '/tmp/extracted.txt'
        # Explicit page ceiling avoids unbounded work on very long documents.
        process = subprocess.run(['/usr/bin/pdftotext','-f','1','-l','60','-enc','UTF-8','-layout',path,output],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
        if process.returncode:
            raise ValueError('PDF 损坏、已加密或无法解析；请解除加密后重试')
        text = Path(output).read_text(encoding='utf-8')
        note = '提取 PDF 前 60 页的文字层；图片、复杂排版和后续页面不在此次提取范围。'
    elif suffix == '.doc':
        output = Path('/tmp/extracted.txt')
        with output.open('wb') as target:
            process = subprocess.run(['/usr/bin/antiword','-m','UTF-8.txt',path],stdin=subprocess.DEVNULL,
                stdout=target,stderr=subprocess.DEVNULL,timeout=10)
        if process.returncode:
            raise ValueError('旧版 DOC 无法解析，请另存为 DOCX 后上传')
        text = output.read_text(encoding='utf-8')
        note = '提取旧版 Word 正文；建议使用 DOCX 以获得更完整的文字。'
    else:
        raise ValueError('文档类型不受支持')
    text = text.replace('\x00','').strip()
    if not text:
        raise ValueError('没有提取到文字；扫描版 PDF 请先 OCR，或将页面作为图片上传')
    truncated = truncated or len(text) > MAX_CHARS
    return {'text': text[:MAX_CHARS], 'truncated': truncated,
            'note': note + (' 正文已截断至 120000 字符。' if truncated else '')}


if __name__ == '__main__':
    resource.setrlimit(resource.RLIMIT_AS, (512*1024**2,512*1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (10,10))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2*1024**2,2*1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32,32))
    try:
        print(json.dumps(extract(sys.argv[1],sys.argv[2]),ensure_ascii=False))
    except (ValueError, OSError, KeyError, zipfile.BadZipFile, ET.ParseError, subprocess.TimeoutExpired) as error:
        message = str(error) if isinstance(error, ValueError) else '文档损坏、结构异常或解析超时'
        print(json.dumps({'error':message},ensure_ascii=False))
        raise SystemExit(2)
