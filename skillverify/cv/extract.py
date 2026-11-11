"""CV file -> text and hyperlinks (SkillVerify, Step 4). PDF, Word .docx and plain .txt.

Only the given file is read. Nothing here prints, logs or returns the file's path. Scanned
(image-only) CVs have no text layer and are reported as ``no_text``: there is no OCR in this step.
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

# --- Limits (change them here, nowhere else) -----------------------------
MAX_FILE_BYTES = 10_000_000               # bigger files are refused (too_large)
MAX_PAGES = 20                            # PDF pages read; the rest are ignored
MAX_CV_CHARS = 30000                      # longer text is cut and flagged truncated
MAX_DOCX_UNCOMPRESSED_BYTES = 50_000_000  # zip-bomb guard for .docx
MAX_LINKS = 100                           # hyperlink targets kept
MIN_TEXT_CHARS = 30                       # fewer non-space characters means "no text"

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f﻿]")
_SUPPORTED = {".pdf": "pdf", ".docx": "docx", ".txt": "txt"}


class CVExtractError(Exception):
    """A CV file that cannot be turned into text. ``code`` is one of: unsupported_file,
    unreadable_file, encrypted_file, no_text, too_large."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def clean_text(text: str) -> str:
    """Drop NUL/control characters (keep newlines and tabs) and collapse runs of blank lines."""
    text = _CONTROL_CHARS.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _unique_links(raw_links: list[Any]) -> list[str]:
    links: list[str] = []
    for link in raw_links:
        if isinstance(link, str):
            link = _CONTROL_CHARS.sub("", link).strip()
            if link and link not in links:
                links.append(link)
        if len(links) >= MAX_LINKS:
            break
    return links


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def _pdf_links(page: Any) -> list[str]:
    """Hyperlink targets of one page (/Annots -> /A -> /URI). A CV often shows only the word
    "GitHub" while the real URL exists only in the link."""
    links: list[str] = []
    annotations = page.get("/Annots")
    if not annotations:
        return links
    for reference in annotations.get_object():
        action = reference.get_object().get("/A")
        if action is None:
            continue
        uri = action.get_object().get("/URI")
        if isinstance(uri, str):
            links.append(uri)
    return links


def _read_pdf(data: bytes) -> tuple[str, list[str], int]:
    import pypdf  # imported here so the other formats work even if a library is missing
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)  # pypdf logs warnings about odd files; we report errors ourselves

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")  # a PDF that opens with an empty password may be read
            except Exception:  # noqa: BLE001 - e.g. a missing crypto library: we never try to crack anything
                opened = 0
            if not opened:
                raise CVExtractError("encrypted_file", "The PDF is password-protected. Remove the password and try again.")
        total_pages = len(reader.pages)
        texts: list[str] = []
        links: list[str] = []
        for page in list(reader.pages)[:MAX_PAGES]:
            texts.append(page.extract_text() or "")
            try:
                links.extend(_pdf_links(page))
            except Exception:  # noqa: BLE001 - a malformed link must not lose the text
                pass
    except CVExtractError:
        raise
    except Exception:  # noqa: BLE001 - corrupt PDFs raise many kinds of errors
        raise CVExtractError("unreadable_file", "The PDF could not be read (it may be corrupt).") from None
    return "\n".join(texts), links, total_pages


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------
def _docx_links(part: Any) -> list[str]:
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    return [rel.target_ref for rel in part.rels.values() if rel.reltype == RT.HYPERLINK and rel.is_external]


def _read_docx(data: bytes) -> tuple[str, list[str]]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(item.file_size for item in archive.infolist()) > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise CVExtractError("unreadable_file", "The Word file expands to an unreasonable size.")
        document = Document(io.BytesIO(data))

        lines: list[str] = []
        seen_extra: set[str] = set()
        headers = []
        footers = []
        for section in document.sections:
            for target, bucket in ((section.header, headers), (section.footer, footers)):
                text = "\n".join(p.text for p in target.paragraphs if p.text.strip())
                if text and text not in seen_extra:  # linked headers repeat on every section
                    seen_extra.add(text)
                    bucket.append(text)
        lines.extend(headers)
        for child in document.element.body.iterchildren():  # paragraphs and tables, in document order
            if child.tag.endswith("}p"):
                lines.append(Paragraph(child, document).text)
            elif child.tag.endswith("}tbl"):
                for row in Table(child, document).rows:
                    cells: list[str] = []
                    seen_cells: set[int] = set()
                    for cell in row.cells:
                        if id(cell._tc) not in seen_cells:  # a merged cell is returned once per column
                            seen_cells.add(id(cell._tc))
                            if cell.text.strip():
                                cells.append(cell.text.strip())
                    lines.append(" | ".join(cells))
        lines.extend(footers)

        links = _docx_links(document.part)
        for section in document.sections:
            links.extend(_docx_links(section.header.part))
            links.extend(_docx_links(section.footer.part))
    except CVExtractError:
        raise
    except Exception:  # noqa: BLE001 - python-docx raises many kinds of errors for broken files
        raise CVExtractError("unreadable_file", "The Word file could not be read (it may be corrupt).") from None
    return "\n".join(lines), links


# --------------------------------------------------------------------------
# the public function
# --------------------------------------------------------------------------
def extract_text(path: str | Path) -> dict[str, Any]:
    """Read a CV file and return ``{"text", "links", "format", "pages", "characters", "truncated"}``.

    Raises ``CVExtractError`` (see its docstring for the codes). The format is decided by the
    extension AND the file's first bytes.
    """
    file_path = Path(path)
    extension = file_path.suffix.lower()
    if extension not in _SUPPORTED:
        hint = " Save it as .docx or PDF and try again." if extension == ".doc" else ""
        raise CVExtractError("unsupported_file", f"Unsupported file type '{extension or 'none'}'.{hint}"
                                                 " Supported: .pdf, .docx, .txt.")
    try:
        if not file_path.is_file():
            raise CVExtractError("unreadable_file", "The file was not found (or is not a file).")
        size = file_path.stat().st_size
        if size == 0:
            raise CVExtractError("unreadable_file", "The file is empty.")
        if size > MAX_FILE_BYTES:
            raise CVExtractError("too_large", f"The file is larger than {MAX_FILE_BYTES // 1_000_000} MB.")
        data = file_path.read_bytes()
    except OSError:
        raise CVExtractError("unreadable_file", "The file could not be read.") from None

    kind = _SUPPORTED[extension]
    pages = None
    if kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise CVExtractError("unsupported_file", "The file has a .pdf extension but is not a PDF.")
        text, raw_links, pages = _read_pdf(data)
    elif kind == "docx":
        if not data.startswith(b"PK"):
            raise CVExtractError("unsupported_file", "The file has a .docx extension but is not a Word document.")
        text, raw_links = _read_docx(data)
    else:
        if data.startswith((b"%PDF-", b"PK\x03\x04")):
            raise CVExtractError("unsupported_file", "The file has a .txt extension but is a PDF or Word file.")
        try:
            text = data.decode("utf-8-sig")  # also strips a BOM
        except UnicodeDecodeError:
            text = data.decode("latin-1")  # never fails
        raw_links = []

    text = clean_text(text)
    if len(re.sub(r"\s+", "", text)) < MIN_TEXT_CHARS:
        raise CVExtractError(
            "no_text", "No readable text was found. This looks like a scanned or image-only CV; "
                       "there is no OCR in this step. Use a text-based PDF or a .docx file.")
    truncated = len(text) > MAX_CV_CHARS
    text = text[:MAX_CV_CHARS]
    return {"text": text, "links": _unique_links(raw_links), "format": kind, "pages": pages,
            "characters": len(text), "truncated": truncated}
