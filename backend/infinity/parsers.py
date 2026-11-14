"""Parsers that turn uploaded technical files into locator-carrying chunks.

PDF parsing reuses the project's existing PyMuPDF loader, normalizer and
section-aware chunker so page provenance stays identical to the legacy index.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from .chunking import ParsedChunk, ParsedDocument, split_text

SUPPORTED_SUFFIXES = {
    ".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".csv", ".tsv",
    ".txt", ".md", ".log", ".json", ".xml", ".html", ".htm",
}
ROWS_PER_CHUNK = 25


class UnsupportedFormat(ValueError):
    pass


def parse_file(path: str | Path, filename: str | None = None) -> ParsedDocument:
    path = Path(path)
    name = filename or path.name
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedFormat(f"unsupported file type {suffix or '(none)'}")
    title = Path(name).stem
    if suffix == ".pdf":
        return _parse_pdf(path, title)
    if suffix == ".docx":
        return _parse_docx(path, title)
    if suffix in {".xlsx", ".xlsm"}:
        return _parse_xlsx(path, title)
    if suffix == ".pptx":
        return _parse_pptx(path, title)
    if suffix in {".csv", ".tsv"}:
        return _parse_csv(path, title, "\t" if suffix == ".tsv" else ",")
    if suffix in {".html", ".htm"}:
        from .htmltext import html_to_text
        page_title, text, _links = html_to_text(_read_text(path))
        return _text_document(page_title or title, "html", text)
    if suffix == ".json":
        try:
            text = json.dumps(json.loads(_read_text(path)), indent=2, ensure_ascii=False)
        except ValueError:
            text = _read_text(path)
        return _text_document(title, "json", text)
    return _text_document(title, suffix.lstrip("."), _read_text(path))


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1256", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _text_document(title: str, doc_type: str, text: str) -> ParsedDocument:
    document = ParsedDocument(title=title, doc_type=doc_type)
    for index, piece in enumerate(split_text(text), start=1):
        document.chunks.append(ParsedChunk(text=piece, locator={"type": "text", "part": index}))
    if not document.chunks:
        document.issues.append("file contains no extractable text")
    return document


def _parse_pdf(path: Path, title: str) -> ParsedDocument:
    from app.ingestion.chunker import SemanticChunker
    from app.ingestion.document_parser import normalize_document
    from app.ingestion.pdf_loader import PyMuPDFLoader

    loaded = normalize_document(PyMuPDFLoader().load(path))
    chunks = SemanticChunker(chunk_size=1600, overlap=200).chunk_pages(
        list(loaded.pages),
        document_id=loaded.document_id,
        document_name=loaded.document_name,
        document_type="pdf",
        source_file=str(path),
        document_version=loaded.document_version,
        ingestion_date=loaded.ingestion_date,
    )
    document = ParsedDocument(
        title=loaded.document_name or title, doc_type="pdf", page_count=loaded.page_count,
        metadata={"version": loaded.document_version, "extraction_method": loaded.extraction_method},
    )
    document.issues = [f"{i.stage}: {i.message}" for i in loaded.issues]
    for chunk in chunks:
        pages = chunk.metadata.source_pages
        document.chunks.append(
            ParsedChunk(
                text=chunk.text,
                section=chunk.metadata.section_title,
                locator={
                    "type": "pdf",
                    "pdf_pages": list(dict.fromkeys(p.pdf_page for p in pages)) or [chunk.metadata.page_number],
                    "printed_pages": list(dict.fromkeys(p.printed_page for p in pages if p.printed_page)),
                },
            )
        )
    if not document.chunks:
        document.issues.append("PDF produced no text (scanned PDF? OCR is not included)")
    return document


def _md_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [[(c or "").replace("\n", " ").replace("|", "/").strip() for c in r] + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(norm[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in norm[1:]]
    return "\n".join(lines)


def _parse_docx(path: Path, title: str) -> ParsedDocument:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    document = ParsedDocument(title=title, doc_type="docx")
    core_title = (doc.core_properties.title or "").strip()
    if core_title:
        document.title = core_title
    section: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        text = "\n\n".join(buffer).strip()
        buffer.clear()
        for piece in split_text(text):
            document.chunks.append(ParsedChunk(text=piece, section=section, locator={"type": "docx", "section": section}))

    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, doc)
            text = paragraph.text.strip()
            if not text:
                continue
            if (paragraph.style.name or "").lower().startswith(("heading", "title")):
                flush()
                section = text
                buffer.append(text)
            else:
                buffer.append(text)
        elif tag == "tbl":
            table = Table(child, doc)
            rows = [[cell.text for cell in row.cells] for row in table.rows]
            md = _md_table(rows)
            if md:
                buffer.append(md)
    flush()
    if not document.chunks:
        document.issues.append("document contains no extractable text")
    return document


def _row_blocks(header: list[str], rows: list[tuple[int, list[str]]], make_locator) -> list[ParsedChunk]:
    chunks: list[ParsedChunk] = []
    for start in range(0, len(rows), ROWS_PER_CHUNK):
        block = rows[start:start + ROWS_PER_CHUNK]
        table = _md_table([header] + [r for _, r in block])
        for piece in split_text(table, size=3000, overlap=200) if len(table) > 3000 else [table]:
            chunks.append(ParsedChunk(text=piece, locator=make_locator(block[0][0], block[-1][0])))
    return chunks


def _parse_xlsx(path: Path, title: str) -> ParsedDocument:
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    document = ParsedDocument(title=title, doc_type="xlsx")
    try:
        for sheet in workbook.worksheets:
            header: list[str] | None = None
            body: list[tuple[int, list[str]]] = []
            for number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                cells = ["" if v is None else str(v) for v in row]
                if not any(c.strip() for c in cells):
                    continue
                if header is None:
                    header = cells
                    continue
                body.append((number, cells))
            if header is None:
                continue
            if not body:
                body = [(1, header)]
                header = [f"col{i + 1}" for i in range(len(header))]
            for chunk in _row_blocks(header, body, lambda a, b, s=sheet.title: {"type": "xlsx", "sheet": s, "row_start": a, "row_end": b}):
                chunk.text = f"Sheet: {sheet.title}\n{chunk.text}"
                document.chunks.append(chunk)
    finally:
        workbook.close()
    if not document.chunks:
        document.issues.append("workbook contains no data")
    return document


def _parse_csv(path: Path, title: str, delimiter: str) -> ParsedDocument:
    reader = csv.reader(io.StringIO(_read_text(path)), delimiter=delimiter)
    header: list[str] | None = None
    body: list[tuple[int, list[str]]] = []
    for number, row in enumerate(reader, start=1):
        if not any(c.strip() for c in row):
            continue
        if header is None:
            header = row
        else:
            body.append((number, row))
    document = ParsedDocument(title=title, doc_type="csv")
    if header is not None and body:
        document.chunks = _row_blocks(header, body, lambda a, b: {"type": "csv", "row_start": a, "row_end": b})
    else:
        document.issues.append("CSV contains no data rows")
    return document


def _parse_pptx(path: Path, title: str) -> ParsedDocument:
    from pptx import Presentation

    presentation = Presentation(str(path))
    document = ParsedDocument(title=title, doc_type="pptx", page_count=len(presentation.slides))
    for number, slide in enumerate(presentation.slides, start=1):
        parts: list[str] = []
        slide_title = slide.shapes.title.text.strip() if slide.shapes.title is not None and slide.shapes.title.has_text_frame else None
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text.strip())
            elif getattr(shape, "has_table", False) and shape.has_table:
                parts.append(_md_table([[c.text for c in r.cells] for r in shape.table.rows]))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append("Speaker notes: " + notes)
        text = "\n\n".join(parts).strip()
        for piece in split_text(text):
            document.chunks.append(ParsedChunk(text=piece, section=slide_title, locator={"type": "pptx", "slide": number}))
    if not document.chunks:
        document.issues.append("presentation contains no text")
    return document
