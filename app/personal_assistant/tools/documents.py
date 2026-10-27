"""Deterministic document readers/writers with no AI or cloud services.

Supported:
  * UTF-8/cp1252 text formats (.txt, .md, .json, .csv, source code, etc.)
  * PDF text extraction via the local pypdf library (read only)
  * DOCX text extraction and minimal DOCX creation using the ZIP/XML format
    directly (no Word installation and no cloud conversion)
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_W_NS}}}"
_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""


def read_document(path: Path, max_bytes: int = 2_000_000) -> tuple[str, str]:
    """Return ``(text, format)``. Raises ValueError for unsupported/failed formats."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path, max_bytes), "pdf"
    if suffix == ".docx":
        return _read_docx(path), "docx"
    return _read_text(path, max_bytes), "text"


def _read_text(path: Path, max_bytes: int) -> str:
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"File is larger than the configured read limit ({max_bytes} bytes).")
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        raise ValueError("This appears to be a binary file; use a supported document format.")
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("The text encoding is not supported (tried UTF-8 and Windows-1252).")


def _read_pdf(path: Path, max_bytes: int) -> str:
    if path.stat().st_size > max_bytes * 5:
        raise ValueError("PDF is too large for the configured local extraction limit.")
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF support is unavailable: the local pypdf package is not installed.") from exc
    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise ValueError("The PDF is encrypted and cannot be read without a password.") from exc
        pages = min(len(reader.pages), 300)
        parts: list[str] = []
        total = 0
        for page in reader.pages[:pages]:
            text = page.extract_text() or ""
            parts.append(text)
            total += len(text)
            if total > max_bytes:
                parts.append("\n...[truncated]")
                break
        if not any(part.strip() for part in parts):
            raise ValueError("No selectable text was found in this PDF (it may be a scanned image).")
        return "\n\n".join(parts)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"PDF extraction failed: {exc.__class__.__name__}") from exc


def _read_docx(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            member = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ValueError("This is not a readable DOCX document.") from exc
    try:
        root = ET.fromstring(member)
    except ET.ParseError as exc:
        raise ValueError("The DOCX document XML is invalid.") from exc
    paragraphs: list[str] = []
    for para in root.iter(f"{_W}p"):
        text = "".join(node.text or "" for node in para.iter(f"{_W}t"))
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def write_docx(path: Path, text: str) -> None:
    paragraphs = text.splitlines() or [""]
    body = []
    for line in paragraphs:
        body.append(
            "<w:p><w:r><w:t xml:space=\"preserve\">"
            + escape(line)
            + "</w:t></w:r></w:p>"
        )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W_NS}"><w:body>'
        + "".join(body)
        + '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
        + "</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("word/document.xml", document)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.getvalue())


def supported_write_suffixes() -> set[str]:
    return {
        ".txt", ".md", ".json", ".jsonl", ".csv", ".log", ".py", ".ps1", ".yaml",
        ".yml", ".toml", ".ini", ".cfg", ".html", ".htm", ".js", ".ts", ".css",
        ".java", ".c", ".h", ".cpp", ".cs", ".sql", ".xml", ".docx",
    }


def looks_like_instruction_injection(text: str) -> bool:
    """Detect untrusted content that appears to instruct the assistant.

    This is a signal for the UI and planner, not a security boundary. The real
    boundary is that page/file content is never fed to a tool executor directly.
    """
    patterns = [
        r"(?i)\b(ignore|disregard)\b.{0,40}\b(previous|above|system)\b.{0,20}\b(instructions?|prompt)",
        r"(?i)\b(run|execute|delete|upload|send)\b.{0,40}\b(files?|folder|password|token|secret)\b",
        r"(?i)\byou are now\b|\bnew system prompt\b|\bdeveloper mode\b",
        r"(?i)\b(assistant|ai)\b.{0,30}\b(must|should)\b.{0,30}\b(delete|send|upload|execute)\b",
    ]
    return any(re.search(pattern, text) for pattern in patterns)
