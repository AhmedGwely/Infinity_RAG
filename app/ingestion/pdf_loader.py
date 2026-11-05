"""Validated, page-aware PDF loading with PyMuPDF."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from app.core.exceptions import PDFExtractionError, PDFValidationError, PasswordProtectedPDFError
from app.core.schemas import (
    BatchLoadResult,
    ExtractedTable,
    ExtractionIssue,
    PDFDocument,
    PageText,
)

logger = logging.getLogger(__name__)
_DEGENERATE_EMPTY_COLUMN_MAX_WIDTH_PT = 3.0
_VERSION_PATTERN = re.compile(r"\b(?:v|version\s*)(\d+(?:\.\d+)+[A-Za-z0-9.-]*)\b", re.IGNORECASE)
_PRINTED_PAGE_PATTERN = re.compile(
    r"\b(?:page|pg\.?|p\.?)\s*(?:no\.?\s*)?(\d+)(?:\s*(?:of|/)\s*\d+)?\s*$",
    re.IGNORECASE,
)


class PDFLoader(Protocol):
    """Interface for validated PDF loading and batch processing."""

    def load(self, source: str | Path) -> PDFDocument:
        """Open a PDF and create one record for each physical page."""
        ...

    def load_batch(self, directory: str | Path) -> BatchLoadResult:
        """Load every PDF in a directory while retaining per-file errors."""
        ...


class PyMuPDFLoader:
    """Load PDF metadata, page text, and detectable table structure."""

    def load(self, source: str | Path) -> PDFDocument:
        """Validate and extract a PDF without dropping failed pages."""
        source_path = Path(source).expanduser()
        if not source_path.exists():
            raise PDFValidationError(f"PDF does not exist: {source_path}")
        if not source_path.is_file():
            raise PDFValidationError(f"PDF path is not a file: {source_path}")
        if source_path.suffix.lower() != ".pdf":
            raise PDFValidationError(f"Expected a .pdf file: {source_path}")
        try:
            if source_path.stat().st_size == 0:
                raise PDFValidationError(f"PDF is empty: {source_path}")
        except OSError as error:
            raise PDFValidationError(f"Cannot read PDF file {source_path}: {error}") from error

        try:
            import fitz
        except ImportError as error:
            raise PDFValidationError(
                "PyMuPDF is not installed. Install the declared project requirements to process PDFs."
            ) from error

        try:
            with fitz.open(str(source_path)) as pdf:
                if pdf.needs_pass:
                    raise PasswordProtectedPDFError(
                        f"PDF is password-protected and no password was supplied: {source_path.name}"
                    )
                if len(pdf) == 0:
                    raise PDFValidationError(f"PDF contains no pages: {source_path}")

                file_metadata = {
                    str(key): str(value).strip()
                    for key, value in (pdf.metadata or {}).items()
                    if value is not None and str(value).strip()
                }
                document_id = hashlib.sha256(source_path.read_bytes()).hexdigest()
                document_name = source_path.stem
                document_version = self._find_version(document_name, file_metadata)
                pages: list[PageText] = []
                issues: list[ExtractionIssue] = []

                for zero_index, pdf_page in enumerate(pdf, start=1):
                    try:
                        text = pdf_page.get_text("text", sort=True) or ""
                        issue_count = len(issues)
                        printed_page = detect_printed_page(text)
                        tables = self._extract_tables(
                            pdf_page,
                            zero_index,
                            issues,
                            printed_page=printed_page,
                        )
                        table_extraction_failed = any(
                            issue.pdf_page == zero_index and issue.stage.startswith("pymupdf_table")
                            for issue in issues[issue_count:]
                        )
                        has_text = bool(text.strip())
                        page = PageText(
                            pdf_page=zero_index,
                            text=text,
                            document_id=document_id,
                            document_name=document_name,
                            document_version=document_version,
                            printed_page=printed_page,
                            extraction_method="pymupdf",
                            content_type="text" if has_text else "empty",
                            has_tables=bool(tables),
                            needs_review=(
                                not has_text
                                or table_extraction_failed
                                or any(table.needs_review for table in tables)
                            ),
                            tables=tuple(tables),
                        )
                    except Exception as error:
                        message = f"PyMuPDF failed to extract page {zero_index}: {error}"
                        logger.exception(message)
                        issues.append(
                            ExtractionIssue(
                                stage="pymupdf_page_extraction",
                                message=message,
                                pdf_page=zero_index,
                                severity="error",
                            )
                        )
                        page = PageText(
                            pdf_page=zero_index,
                            text="",
                            document_id=document_id,
                            document_name=document_name,
                            document_version=document_version,
                            extraction_method="pymupdf_failed",
                            content_type="empty",
                            needs_review=True,
                            extraction_error=message,
                        )
                    pages.append(page)

                if len(pages) != len(pdf):
                    raise PDFExtractionError(
                        f"Page accounting mismatch for {source_path.name}: "
                        f"expected {len(pdf)}, recorded {len(pages)}"
                    )

                return PDFDocument(
                    document_id=document_id,
                    document_name=document_name,
                    document_version=document_version,
                    source_file=source_path.resolve(),
                    page_count=len(pdf),
                    file_metadata=file_metadata,
                    ingestion_date=datetime.now(timezone.utc),
                    pages=tuple(pages),
                    issues=tuple(issues),
                    extraction_method="pymupdf",
                )
        except (PDFValidationError, PDFExtractionError):
            raise
        except Exception as error:
            raise PDFValidationError(
                f"PDF is unreadable or corrupted ({source_path.name}): {error}"
            ) from error

    def load_batch(self, directory: str | Path) -> BatchLoadResult:
        """Load sorted PDFs from a directory and report files that failed."""
        directory_path = Path(directory).expanduser()
        if not directory_path.exists() or not directory_path.is_dir():
            raise PDFValidationError(f"Incoming PDF directory does not exist: {directory_path}")

        documents: list[PDFDocument] = []
        issues: list[ExtractionIssue] = []
        for source in sorted(directory_path.iterdir(), key=lambda path: path.name.casefold()):
            if not source.is_file() or source.suffix.lower() != ".pdf":
                continue
            try:
                documents.append(self.load(source))
            except PDFValidationError as error:
                logger.error("Skipping invalid PDF %s: %s", source.name, error)
                issues.append(
                    ExtractionIssue(
                        stage="pymupdf_file_validation",
                        message=f"{source.name}: {error}",
                        severity="error",
                    )
                )
        return BatchLoadResult(documents=tuple(documents), issues=tuple(issues))

    @staticmethod
    def _find_version(document_name: str, file_metadata: dict[str, str]) -> str | None:
        """Use explicit filename or metadata version strings; never infer one."""
        match = _VERSION_PATTERN.search(document_name)
        if match:
            return match.group(1)
        for key in ("version", "subject"):
            match = _VERSION_PATTERN.search(file_metadata.get(key, ""))
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _extract_tables(
        pdf_page,
        page_number: int,
        issues: list[ExtractionIssue],
        *,
        printed_page: int | None,
    ) -> list[ExtractedTable]:
        """Use PyMuPDF table detection as a structured fallback where available."""
        find_tables = getattr(pdf_page, "find_tables", None)
        if find_tables is None:
            return []
        try:
            page_tables = find_tables().tables
        except Exception as error:
            issues.append(
                ExtractionIssue(
                    stage="pymupdf_table_detection",
                    message=f"Table detection failed on page {page_number}: {error}",
                    pdf_page=page_number,
                )
            )
            return []

        tables: list[ExtractedTable] = []
        for table_index, table in enumerate(page_tables, start=1):
            try:
                extracted_rows = table.extract() or []
                clean_rows = tuple(
                    tuple(_clean_cell(cell) for cell in row)
                    for row in extracted_rows
                    if row is not None
                )
                header_info = getattr(table, "header", None)
                header = getattr(header_info, "names", None)
                headers = tuple(_clean_cell(cell) for cell in header) if header else ()
                if not any(headers):
                    headers = ()
                headers, clean_rows = _remove_degenerate_empty_columns(
                    table,
                    headers,
                    clean_rows,
                )
                rows = clean_rows
                if not headers and clean_rows:
                    headers, rows = clean_rows[0], clean_rows[1:]
                elif (
                    not getattr(header_info, "external", False)
                    and clean_rows
                    and clean_rows[0] == headers
                ):
                    rows = clean_rows[1:]
                review = not headers or not rows or any(len(row) != len(headers) for row in rows)
                tables.append(
                    ExtractedTable(
                        table_id=f"page-{page_number}-table-{table_index}",
                        pdf_page=page_number,
                        printed_page=printed_page,
                        caption=None,
                        column_headers=headers,
                        rows=rows,
                        markdown=table_to_markdown(headers, rows),
                        needs_review=review,
                    )
                )
                if review:
                    issues.append(
                        ExtractionIssue(
                            stage="pymupdf_table_validation",
                            message=f"Table {table_index} on page {page_number} has uncertain headers or row shape.",
                            pdf_page=page_number,
                        )
                    )
            except Exception as error:
                issues.append(
                    ExtractionIssue(
                        stage="pymupdf_table_extraction",
                        message=f"Table {table_index} on page {page_number} could not be preserved: {error}",
                        pdf_page=page_number,
                        severity="error",
                    )
                )
        return tables


def detect_printed_page(text: str) -> int | None:
    """Detect only explicitly labeled printed page numbers near the page end."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates = {
        int(match.group(1))
        for line in lines[-5:]
        if (match := _PRINTED_PAGE_PATTERN.search(line)) is not None
    }
    return candidates.pop() if len(candidates) == 1 else None


def table_to_markdown(headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> str:
    """Render a stable Markdown table while retaining every cell value."""
    if not headers:
        return ""
    escape = lambda value: value.replace("|", "\\|").replace("\n", "<br>")
    header_line = "| " + " | ".join(escape(value) for value in headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    row_lines = ["| " + " | ".join(escape(value) for value in row) + " |" for row in rows]
    return "\n".join((header_line, separator, *row_lines))


def _clean_cell(value: object) -> str:
    """Convert a parsed cell to text without altering engineering notation."""
    return "" if value is None else str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _remove_degenerate_empty_columns(
    table,
    headers: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Drop only all-empty columns whose measured cells are at most three points wide."""
    column_count = getattr(table, "col_count", 0)
    row_count = getattr(table, "row_count", 0)
    cells = getattr(table, "cells", None)
    if (
        not isinstance(column_count, int)
        or column_count <= 0
        or not isinstance(row_count, int)
        or row_count <= 0
        or not isinstance(cells, (list, tuple))
        or len(cells) != row_count * column_count
        or len(headers) != column_count
        or any(len(row) != column_count for row in rows)
    ):
        return headers, rows

    remove_columns: set[int] = set()
    for column_index in range(column_count):
        if headers[column_index] or any(row[column_index] for row in rows):
            continue
        column_cells = [cells[row_index * column_count + column_index] for row_index in range(row_count)]
        if any(cell is None or len(cell) < 4 for cell in column_cells):
            continue
        widths = [float(cell[2]) - float(cell[0]) for cell in column_cells]
        if all(0 <= width <= _DEGENERATE_EMPTY_COLUMN_MAX_WIDTH_PT for width in widths):
            remove_columns.add(column_index)

    if not remove_columns:
        return headers, rows
    kept_columns = [index for index in range(column_count) if index not in remove_columns]
    return (
        tuple(headers[index] for index in kept_columns),
        tuple(tuple(row[index] for index in kept_columns) for row in rows),
    )
