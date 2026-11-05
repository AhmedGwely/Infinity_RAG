"""Structured document parsing with an explicit PyMuPDF fallback."""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import replace
from typing import Protocol

from app.core.schemas import ContentBlock, ExtractedTable, ExtractionIssue, PDFDocument, PageText
from app.ingestion.pdf_loader import detect_printed_page, table_to_markdown

logger = logging.getLogger(__name__)
_SECTION_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)*(?:[A-Za-z])?)\s*[.)]?\s+(.+?)\s*$")


class DocumentParser(Protocol):
    """Interface for replaceable structured document parsers."""

    def parse(self, document: PDFDocument) -> PDFDocument:
        """Enrich page text with ordered structural blocks and tables."""
        ...


class DoclingDocumentParser:
    """Extract headings, lists, paragraphs, captions, and tables with Docling."""

    def __init__(self, *, offline: bool = True) -> None:
        self.offline = offline

    def parse(self, document: PDFDocument) -> PDFDocument:
        """Parse a PDF, using PyMuPDF page data if Docling cannot complete."""
        if self.offline:
            os.environ["HF_HUB_OFFLINE"] = "1"

        try:
            from docling.document_converter import DocumentConverter
        except ImportError as error:
            return self._fallback(document, f"Docling is not installed: {error}")

        result = None
        try:
            result = DocumentConverter().convert(str(document.source_file))
            return self._convert_items(document, result.document)
        except Exception as error:
            logger.exception("Docling conversion failed for %s", document.document_name)
            return self._fallback(document, f"Docling conversion failed: {error}")
        finally:
            release = getattr(getattr(result, "input", None), "release", None)
            if callable(release):
                try:
                    release()
                except Exception:
                    logger.warning("Could not release Docling input for %s", document.document_name)

    def _convert_items(self, document: PDFDocument, docling_document) -> PDFDocument:
        """Map Docling's ordered document items back to physical PDF pages."""
        page_blocks: dict[int, list[ContentBlock]] = {page.pdf_page: [] for page in document.pages}
        page_tables: dict[int, list[ExtractedTable]] = {page.pdf_page: [] for page in document.pages}
        page_sections: dict[int, tuple[str | None, str | None]] = {}
        issues = list(document.issues)
        table_review_pages: set[int] = set()
        current_section_number: str | None = None
        current_section_title: str | None = None
        pending_caption: str | None = None

        try:
            for item, _level in docling_document.iterate_items():
                label = _item_label(item)
                text = _item_text(item)
                page_number = _item_page(item)
                if page_number not in page_blocks:
                    issues.append(
                        ExtractionIssue(
                            stage="docling_provenance",
                            message=f"Docling item has missing or out-of-range page provenance: {page_number!r}",
                        )
                    )
                    continue

                if label in {"section_header", "title"}:
                    current_section_title = text or None
                    section_match = _SECTION_PATTERN.match(text)
                    current_section_number = section_match.group(1) if section_match else None
                    page_sections[page_number] = (current_section_number, current_section_title)
                    page_blocks[page_number].append(
                        ContentBlock(
                            content_type="heading",
                            text=text,
                            pdf_page=page_number,
                            section_number=current_section_number,
                            section_title=current_section_title,
                        )
                    )
                    pending_caption = None
                    continue

                page_sections.setdefault(page_number, (current_section_number, current_section_title))

                if label == "table":
                    try:
                        table_index = sum(len(tables) for tables in page_tables.values()) + 1
                        table = _docling_table(
                            item,
                            docling_document,
                            page_number=page_number,
                            table_index=table_index,
                            printed_page=detect_printed_page(document.pages[page_number - 1].text),
                            caption=pending_caption,
                            section_number=current_section_number,
                            section_title=current_section_title,
                        )
                        page_tables[page_number].append(table)
                        page_blocks[page_number].append(
                            ContentBlock(
                                content_type="table",
                                text=table.markdown,
                                pdf_page=page_number,
                                section_number=current_section_number,
                                section_title=current_section_title,
                                table_id=table.table_id,
                            )
                        )
                        if table.needs_review:
                            issues.append(
                                ExtractionIssue(
                                    stage="docling_table_validation",
                                    message=f"Table {table.table_id} has uncertain headers or row shape.",
                                    pdf_page=page_number,
                                )
                            )
                    except Exception as error:
                        table_review_pages.add(page_number)
                        issues.append(
                            ExtractionIssue(
                                stage="docling_table_extraction",
                                message=f"Docling table on page {page_number} could not be preserved: {error}",
                                pdf_page=page_number,
                                severity="error",
                            )
                        )
                    pending_caption = None
                    continue

                if not text:
                    continue
                if label == "caption":
                    pending_caption = text
                page_blocks[page_number].append(
                    ContentBlock(
                        content_type=_content_type(label),
                        text=text,
                        pdf_page=page_number,
                        section_number=current_section_number,
                        section_title=current_section_title,
                    )
                )
        except Exception as error:
            logger.exception("Could not map Docling document items for %s", document.document_name)
            return self._fallback(document, f"Docling item mapping failed: {error}")

        parsed_pages: list[PageText] = []
        for page in document.pages:
            blocks = page_blocks[page.pdf_page]
            tables = page_tables[page.pdf_page]
            if not blocks and not page.text.strip():
                issues.append(
                    ExtractionIssue(
                        stage="docling_page_accounting",
                        message="No text or structured blocks extracted for this physical PDF page.",
                        pdf_page=page.pdf_page,
                    )
                )
            table_mismatch = bool(tables or page.tables) and len(tables) != len(page.tables)
            if table_mismatch:
                table_review_pages.add(page.pdf_page)
                issues.append(
                    ExtractionIssue(
                        stage="table_crosscheck",
                        message=(
                            f"Docling found {len(tables)} table(s), while PyMuPDF found "
                            f"{len(page.tables)}; inspect this page for missed or split tables."
                        ),
                        pdf_page=page.pdf_page,
                    )
                )
            if table_mismatch:
                fallback_tables = tuple(
                    replace(
                        table,
                        table_id=f"pymupdf-{table.table_id}",
                        printed_page=table.printed_page or detect_printed_page(page.text),
                        section_number=table.section_number or page_sections.get(page.pdf_page, (None, None))[0],
                        section_title=table.section_title or page_sections.get(page.pdf_page, (None, None))[1],
                        needs_review=True,
                    )
                    for table in page.tables
                )
                selected_tables = tuple(tables) + fallback_tables
            else:
                selected_tables = tuple(tables or page.tables)
            section = next((block for block in blocks if block.content_type == "heading"), None)
            page_section_number, page_section_title = page_sections.get(page.pdf_page, (None, None))
            parsed_pages.append(
                replace(
                    page,
                    printed_page=detect_printed_page(page.text),
                    section_number=(
                        section.section_number if section else page_section_number or page.section_number
                    ),
                    section_title=(
                        section.section_title if section else page_section_title or page.section_title
                    ),
                    extraction_method="docling" if blocks else "pymupdf_fallback",
                    content_type=_page_content_type(page.text, blocks, selected_tables),
                    has_tables=bool(selected_tables),
                    needs_review=(
                        page.needs_review
                        or not blocks
                        or (not blocks and not page.text.strip())
                        or page.pdf_page in table_review_pages
                        or any(table.needs_review for table in selected_tables)
                    ),
                    tables=selected_tables,
                    blocks=tuple(blocks),
                )
            )

        return replace(
            document,
            pages=tuple(parsed_pages),
            issues=tuple(issues),
            extraction_method="docling",
        )

    @staticmethod
    def _fallback(document: PDFDocument, reason: str) -> PDFDocument:
        """Keep every PyMuPDF page and make the parser fallback explicit."""
        issue = ExtractionIssue(stage="docling_fallback", message=reason, severity="warning")
        pages: list[PageText] = []
        current_section_number: str | None = None
        current_section_title: str | None = None
        for page in document.pages:
            blocks: list[ContentBlock] = []
            if page.blocks:
                for block in page.blocks:
                    if block.content_type == "heading":
                        current_section_number = block.section_number or current_section_number
                        current_section_title = block.section_title or block.text
                    blocks.append(
                        replace(
                            block,
                            section_number=block.section_number or current_section_number,
                            section_title=block.section_title or current_section_title,
                        )
                    )
            else:
                for paragraph in page.text.split("\n\n"):
                    paragraph = paragraph.strip()
                    if not paragraph:
                        continue
                    first_line, _, remainder = paragraph.partition("\n")
                    section_match = _SECTION_PATTERN.match(first_line)
                    if section_match and len(first_line) <= 180:
                        current_section_number = section_match.group(1)
                        current_section_title = section_match.group(2).strip()
                        blocks.append(
                            ContentBlock(
                                content_type="heading",
                                text=first_line,
                                pdf_page=page.pdf_page,
                                section_number=current_section_number,
                                section_title=current_section_title,
                            )
                        )
                        paragraph = remainder.strip() if remainder else ""
                    if paragraph:
                        blocks.append(
                            ContentBlock(
                                content_type="paragraph",
                                text=paragraph,
                                pdf_page=page.pdf_page,
                                section_number=current_section_number,
                                section_title=current_section_title,
                            )
                        )
            tables = tuple(
                replace(
                    table,
                    printed_page=table.printed_page or detect_printed_page(page.text),
                    section_number=table.section_number or current_section_number,
                    section_title=table.section_title or current_section_title,
                )
                for table in page.tables
            )
            pages.append(
                replace(
                    page,
                    printed_page=detect_printed_page(page.text),
                    section_number=page.section_number or current_section_number,
                    section_title=page.section_title or current_section_title,
                    extraction_method="pymupdf_fallback",
                    needs_review=True,
                    tables=tables,
                    blocks=tuple(blocks),
                )
            )
        return replace(
            document,
            pages=tuple(pages),
            issues=(*document.issues, issue),
            extraction_method="pymupdf_fallback",
        )


def normalize_page(page: PageText) -> PageText:
    """Apply conservative Unicode and whitespace normalization only."""
    tables = tuple(
        replace(
            table,
            caption=_normalize_fragment(table.caption) if table.caption else None,
            column_headers=tuple(_normalize_fragment(cell) for cell in table.column_headers),
            rows=tuple(
                tuple(_normalize_fragment(cell) for cell in row)
                for row in table.rows
            ),
            markdown=table_to_markdown(
                tuple(_normalize_fragment(cell) for cell in table.column_headers),
                tuple(
                    tuple(_normalize_fragment(cell) for cell in row)
                    for row in table.rows
                ),
            ),
        )
        for table in page.tables
    )
    table_map = {table.table_id: table for table in tables}
    blocks = tuple(
        replace(
            block,
            text=(
                table_map[block.table_id].markdown
                if block.content_type == "table" and block.table_id in table_map
                else _normalize_fragment(block.text)
            ),
        )
        for block in page.blocks
    )
    return replace(
        page,
        text=_normalize_fragment(page.text),
        tables=tables,
        blocks=blocks,
    )


def _normalize_fragment(text: str) -> str:
    """Normalize layout whitespace without changing meaningful characters."""
    lines = [re.sub(r"[ \t]+", " ", line).rstrip() for line in text.splitlines()]
    normalized = unicodedata.normalize("NFC", "\n".join(lines))
    return re.sub(r"\n{3,}", "\n\n", normalized).strip()


def normalize_document(document: PDFDocument) -> PDFDocument:
    """Normalize page text while retaining symbols, numbers, and table cells."""
    return replace(document, pages=tuple(normalize_page(page) for page in document.pages))


def _docling_table(
    item,
    document,
    *,
    page_number: int,
    table_index: int,
    printed_page: int | None,
    caption: str | None,
    section_number: str | None,
    section_title: str | None,
) -> ExtractedTable:
    """Convert a Docling table through its public DataFrame export API."""
    dataframe = item.export_to_dataframe(doc=document)
    headers = tuple(str(column).strip() for column in dataframe.columns)
    rows = tuple(
        tuple("" if value is None else str(value).strip() for value in row)
        for row in dataframe.itertuples(index=False, name=None)
    )
    uncertain_headers = not headers or all(
        header.isdigit() or header.lower().startswith("unnamed") for header in headers
    )
    needs_review = uncertain_headers or not rows or any(len(row) != len(headers) for row in rows)
    return ExtractedTable(
        table_id=f"page-{page_number}-table-{table_index}",
        pdf_page=page_number,
        printed_page=printed_page,
        caption=caption,
        column_headers=headers,
        rows=rows,
        markdown=table_to_markdown(headers, rows),
        section_number=section_number,
        section_title=section_title,
        needs_review=needs_review,
    )


def _item_label(item) -> str:
    """Normalize a Docling enum or label to a lowercase string."""
    label = getattr(item, "label", "")
    return str(getattr(label, "value", label)).lower().replace(" ", "_")


def _item_text(item) -> str:
    """Read text while leaving source characters otherwise untouched."""
    return str(getattr(item, "text", "") or "").strip()


def _item_page(item) -> int | None:
    """Return the first 1-indexed Docling page number when available."""
    provenance = getattr(item, "prov", None) or []
    if not provenance:
        return None
    page_number = getattr(provenance[0], "page_no", None)
    return page_number if isinstance(page_number, int) else None


def _content_type(label: str) -> str:
    """Map Docling item labels to stable application block types."""
    if "list" in label:
        return "list_item"
    if "caption" in label:
        return "caption"
    if label in {"footnote", "formula", "code"}:
        return label
    return "paragraph"


def _page_content_type(text: str, blocks: list[ContentBlock], tables: tuple[ExtractedTable, ...]) -> str:
    """Describe whether a page contains text, tables, both, or no content."""
    has_text = bool(text.strip() or any(block.text.strip() for block in blocks))
    has_tables = bool(tables)
    if has_text and has_tables:
        return "mixed"
    if has_tables:
        return "table"
    return "text" if has_text else "empty"
