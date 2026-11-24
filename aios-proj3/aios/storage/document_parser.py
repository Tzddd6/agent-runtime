# Project module contract / 项目模块说明
# 职责：管理受控存储、索引和文档；外部文件内容不作为执行指令。
# 主要接口：docx_text, extract_document_file。
# 来源：架构参考 agiresearch/AIOS；具体借用及第三方说明见 THIRD_PARTY_NOTICES.md。
# 开源：团队有权许可的自研代码及修改采用 MIT；第三方原许可不被覆盖。
# AI辅助与审查边界：见 docs/AI工具使用说明.md；不以自动测试代替人工理解。
"""Bounded DOCX / PDF / DOC text extraction for AIOS Kernel attachments."""
from pathlib import Path
import shutil
import subprocess
from typing import Any, Dict, Tuple
import xml.etree.ElementTree as ET
import zipfile

MAX_CHARS = 120000
MAX_PDF_PAGES = 60


def _extract_pdf_with_pymupdf(path: Path) -> str:
    try:
        import pymupdf as pdf_mod
    except ImportError:
        try:
            import fitz as pdf_mod
        except ImportError as exc:
            raise ValueError("PDF 损坏、已加密或无法解析") from exc
    try:
        with pdf_mod.open(str(path)) as doc:
            if getattr(doc, "is_encrypted", False) or getattr(doc, "needs_pass", False):
                raise ValueError("PDF 损坏、已加密或无法解析")
            chunks = []
            total = 0
            limit = min(len(doc), MAX_PDF_PAGES)
            for page_index in range(limit):
                page_text = doc[page_index].get_text("text") or ""
                chunks.append(page_text)
                total += len(page_text) + 1
                if total > MAX_CHARS + 1:
                    break
            return "\n".join(chunks)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF 损坏、已加密或无法解析") from exc


def _extract_pdf_text(path: Path) -> str:
    out_path = path.with_suffix(".extracted.txt")
    if shutil.which("pdftotext"):
        try:
            process = subprocess.run(
                ["pdftotext", "-f", "1", "-l", str(MAX_PDF_PAGES), "-enc", "UTF-8", "-layout", str(path), str(out_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if process.returncode == 0 and out_path.exists():
                return out_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            try:
                if out_path.exists():
                    out_path.unlink()
            except OSError:
                pass
    return _extract_pdf_with_pymupdf(path)


def docx_text(path: str | Path) -> Tuple[str, bool]:
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if (
            len(entries) > 512
            or sum(e.file_size for e in entries) > 32 * 1024**2
            or len({e.filename for e in entries}) != len(entries)
            or any(e.flag_bits & 1 for e in entries)
            or any(e.filename.startswith("/") or ".." in Path(e.filename).parts for e in entries)
            or any(e.filename.lower().endswith("vbaproject.bin") for e in entries)
        ):
            raise ValueError("Word 压缩包异常、加密或包含宏")
        names = ["word/document.xml"] + sorted(
            e.filename
            for e in entries
            if e.filename.startswith(("word/header", "word/footer")) and e.filename.endswith(".xml")
        )
        if "[Content_Types].xml" not in archive.namelist():
            raise ValueError("不是有效的 DOCX 文档")
        result = []
        characters = 0
        for name in names:
            info = archive.getinfo(name)
            if info.file_size > 4 * 1024**2:
                raise ValueError("Word 正文超过解析上限")
            raw = archive.read(name)
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise ValueError("Word 不允许 XML 外部实体")
            root = ET.fromstring(raw)
            ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            for paragraph in root.iter(ns + "p"):
                text = "".join(
                    (node.text or "")
                    if node.tag == ns + "t"
                    else "\t"
                    if node.tag == ns + "tab"
                    else "\n"
                    if node.tag == ns + "br"
                    else ""
                    for node in paragraph.iter()
                )
                result.append(text)
                characters += len(text) + 1
                if characters > MAX_CHARS:
                    return "\n".join(result), True
        return "\n".join(result), False


def extract_document_file(path: str | Path, suffix: str) -> Dict[str, Any]:
    truncated = False
    if suffix == ".docx":
        text, truncated = docx_text(path)
        note = "提取 Word 正文、表格文字及页眉页脚；不执行宏，不读取外部链接。"
    elif suffix == ".pdf":
        raw_bytes = Path(path).read_bytes()
        if not raw_bytes.startswith(b"%PDF-"):
            raise ValueError("PDF 文件头无效")
        text = _extract_pdf_text(Path(path))
        note = "提取 PDF 前 60 页的文字层。"
    else:
        raise ValueError("文档类型不受支持")
    text = text.replace("\x00", "").strip()
    if not text:
        raise ValueError("没有提取到文字")
    truncated = truncated or len(text) > MAX_CHARS
    return {
        "text": text[:MAX_CHARS],
        "truncated": truncated,
        "note": note + (" 正文已截断至 120000 字符。" if truncated else ""),
    }
