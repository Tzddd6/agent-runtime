"""Private attachments; verified images, UTF-8 text, and DOCX/PDF text extraction for AIOS Kernel."""
import base64
import hashlib
import io
import warnings
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Dict, List, Tuple
import uuid

from aios.storage.chat_store import identity
from aios.storage.database import RuntimeDatabase
from aios.storage.document_parser import extract_document_file

TEXT_SUFFIXES = {".txt", ".md", ".py", ".json", ".csv", ".log"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
DOCUMENT_MEDIA = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def check_text(text: str) -> None:
    if "\x00" in text or re.search(
        r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}", text
    ):
        raise ValueError("拒绝二进制、私钥或疑似API密钥附件")


class AttachmentStore:
    def __init__(self, root: str | Path, database: RuntimeDatabase):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = database

    def upload(self, value: Dict[str, Any]) -> Dict[str, Any]:
        conversation = identity(value.get("conversation_id"))
        name = value.get("name")
        encoded = value.get("data")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 120
            or "/" in name
            or "\\" in name
            or name.startswith(".")
            or any(ord(c) < 32 for c in name)
            or not isinstance(encoded, str)
            or len(encoded) > 12 * 1024**2
        ):
            raise ValueError("附件名称或大小无效")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ValueError("附件编码无效") from None
        if not raw or len(raw) > 8 * 1024**2:
            raise ValueError("附件需为1字节至8MiB")

        suffix = Path(name).suffix.lower()
        width = height = None
        extraction = None
        if suffix in TEXT_SUFFIXES:
            if len(raw) > 1024**2:
                raise ValueError("文本附件超过1MiB")
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                raise ValueError("文本附件需为UTF-8") from None
            check_text(text)
            media = "text/plain"
        elif suffix in DOCUMENT_MEDIA:
            with tempfile.TemporaryDirectory(prefix="aios-doc-") as tmp_dir:
                doc_file = Path(tmp_dir) / f"input{suffix}"
                doc_file.write_bytes(raw)
                extraction = extract_document_file(doc_file, suffix)
                check_text(extraction["text"])
            media = DOCUMENT_MEDIA[suffix]
        elif suffix in IMAGE_SUFFIXES:
            try:
                from PIL import Image, ImageOps
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(raw)) as image:
                        if image.format not in ('PNG', 'JPEG', 'WEBP', 'BMP') or image.width * image.height > 16000000:
                            raise ValueError('Image dimensions exceed limit')
                        image.verify()
                    with Image.open(io.BytesIO(raw)) as image:
                        image = ImageOps.exif_transpose(image)
                        image.thumbnail((1280, 1280))
                        image = image.convert('RGB')
                        width, height = image.size
                        output = io.BytesIO()
                        image.save(output, format='JPEG', quality=75)
                        raw = output.getvalue()
                        if len(raw) > 512 * 1024:
                            raise ValueError('Normalized image exceeds limit')
                media = 'image/jpeg'
            except ImportError:
                raise ValueError('Image decoder Pillow is required') from None
            except Exception:
                raise ValueError('Invalid or oversized image') from None
        else:
            raise ValueError("支持PNG/JPEG/WebP/BMP、PDF、DOCX及UTF-8文本")

        key = uuid.uuid4().hex
        path = self.root / f"{key}.bin"
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as f:
            f.write(raw)

        row: Dict[str, Any] = {
            "id": key,
            "conversation": conversation,
            "name": name,
            "media": media,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "width": width,
            "height": height,
            "created": time.time(),
        }
        if extraction:
            row.update(
                extracted_text=extraction["text"],
                extraction_note=extraction["note"],
                extracted_sha256=hashlib.sha256(extraction["text"].encode("utf-8")).hexdigest(),
            )
        try:
            self.database.save_attachment(row)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

        result = {k: row[k] for k in ("id", "name", "media", "size", "width", "height")}
        if extraction:
            result.update(text_chars=len(extraction["text"]), note=extraction["note"])
        return result

    def read(self, key: str, conversation: str) -> Tuple[Dict[str, Any], bytes]:
        key = identity(key)
        conversation = identity(conversation)
        row = self.database.attachment(key, conversation)
        path = self.root / f"{key}.bin"
        if path.is_symlink():
            raise ValueError("附件不能是符号链接")
        with path.open("rb") as f:
            raw = f.read(8 * 1024**2 + 1)
        if len(raw) > 8 * 1024**2 or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ValueError("附件完整性校验失败")
        if row.get("extracted_text") is not None:
            if hashlib.sha256(row["extracted_text"].encode("utf-8")).hexdigest() != row["extracted_sha256"]:
                raise ValueError("文档文字完整性校验失败")
        return row, raw

    def context(self, keys: List[str], conversation: str) -> Dict[str, Any]:
        if not isinstance(keys, list) or len(keys) > 4 or len(set(keys)) != len(keys):
            raise ValueError("每次最多4个不同附件")
        metadata = []
        for key in keys:
            row, raw = self.read(key, conversation)
            entry = {k: row[k] for k in ("id", "name", "media", "width", "height", "size")}
            if not row["media"].startswith("image/"):
                text = row["extracted_text"] if row.get("extracted_text") is not None else raw.decode("utf-8")
                entry.update(text_excerpt=text[:4000], excerpt_partial=len(text) > 4000)
            metadata.append(entry)
        return {"attachments": metadata}
