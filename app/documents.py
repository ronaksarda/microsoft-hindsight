"""Extract plain text from uploaded grant documents (PDF, DOCX, TXT/MD)."""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocumentError(Exception):
    pass


@dataclass
class DocText:
    text: str
    kind: str
    pages: int | None = None


def _pdf(data: bytes) -> DocText:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover
        raise DocumentError("PDF support is not installed on this server (pip install pypdf).") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise DocumentError("This PDF is password protected. Remove the password and try again.") from exc
        parts = [(page.extract_text() or "") for page in reader.pages]
    except PdfReadError as exc:
        raise DocumentError("This file doesn't look like a valid PDF.") from exc
    text = "\n".join(parts)
    if len(text.strip()) < 20:
        raise DocumentError(
            "No selectable text found. This looks like a scanned PDF. Run it through OCR, or paste the text instead."
        )
    return DocText(text=text, kind="pdf", pages=len(parts))


def _docx(data: bytes) -> DocText:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise DocumentError("This file doesn't look like a valid Word (.docx) document.") from exc
    root = ElementTree.fromstring(xml)
    paras = []
    for p in root.iter(f"{_W}p"):
        runs = [t.text or "" for t in p.iter(f"{_W}t")]
        if runs:
            paras.append("".join(runs))
    return DocText(text="\n".join(paras), kind="docx")


def _plain(data: bytes) -> DocText:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return DocText(text=data.decode(enc), kind="text")
        except UnicodeDecodeError:
            continue
    raise DocumentError("Couldn't read this text file.")  # pragma: no cover  (latin-1 never fails)


def extract_text(filename: str, data: bytes) -> DocText:
    if not data:
        raise DocumentError("The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentError("File is larger than 10 MB.")
    name = (filename or "").lower()
    if data[:5] == b"%PDF-" or name.endswith(".pdf"):
        doc = _pdf(data)
    elif data[:2] == b"PK" or name.endswith(".docx"):
        doc = _docx(data)
    elif name.endswith(".doc"):
        raise DocumentError("Old .doc files aren't supported. Save it as .docx or PDF first.")
    else:
        doc = _plain(data)
    doc.text = re.sub(r"[ \t]+", " ", doc.text).strip()
    if len(doc.text) < 20:
        raise DocumentError("Couldn't find any readable text in this file.")
    return doc
