"""Shared typed data models used across ingestion and retrieval."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ExtractionIssue:
    """A parser or page-level issue that must be visible in the quality report."""

    stage: str
    message: str
    pdf_page: int | None = None
    severity: str = "warning"


@dataclass(frozen=True)
class ExtractedTable:
    """A table with cell structure and its full source provenance."""

    table_id: str
    pdf_page: int
    printed_page: int | None
    caption: str | None
    column_headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    markdown: str
    section_number: str | None = None
    section_title: str | None = None
    needs_review: bool = False


@dataclass(frozen=True)
class ContentBlock:
    """Ordered paragraph, heading, list, caption, or table block."""

    content_type: str
    text: str
    pdf_page: int
    section_number: str | None = None
    section_title: str | None = None
    table_id: str | None = None


@dataclass(frozen=True)
class PageText:
    """Extracted content for one physical PDF page with printed-page provenance."""

    pdf_page: int
    text: str
    section_title: str | None = None
    document_id: str = ""
    document_name: str = ""
    document_version: str | None = None
    printed_page: int | None = None
    section_number: str | None = None
    extraction_method: str = "pymupdf"
    content_type: str = "text"
    has_tables: bool = False
    needs_review: bool = False
    tables: tuple[ExtractedTable, ...] = ()
    blocks: tuple[ContentBlock, ...] = ()
    extraction_error: str | None = None

    @property
    def page_number(self) -> int:
        """Backward-compatible alias for the 1-indexed physical PDF page."""
        return self.pdf_page


@dataclass(frozen=True)
class PDFDocument:
    """Validated PDF metadata and one extracted record per physical page."""

    document_id: str
    document_name: str
    document_version: str | None
    source_file: Path
    page_count: int
    file_metadata: dict[str, str]
    ingestion_date: datetime
    pages: tuple[PageText, ...]
    issues: tuple[ExtractionIssue, ...] = ()
    extraction_method: str = "pymupdf"


@dataclass(frozen=True)
class BatchLoadResult:
    """Batch PDF load results and explicit per-file/page issues."""

    documents: tuple[PDFDocument, ...]
    issues: tuple[ExtractionIssue, ...] = ()


@dataclass(frozen=True)
class PageReference:
    """Physical and optional printed page numbers cited by a chunk."""

    pdf_page: int
    printed_page: int | None = None


@dataclass(frozen=True)
class ChunkMetadata:
    """Required provenance metadata attached to every indexed text chunk."""

    document_id: str
    document_name: str
    document_type: str
    page_number: int
    section_title: str | None
    chunk_id: str
    source_file: str
    ingestion_date: datetime
    document_version: str | None = None
    section_number: str | None = None
    source_pages: tuple[PageReference, ...] = ()


@dataclass(frozen=True)
class DocumentChunk:
    """Text content and provenance for a single retrieval unit."""

    text: str
    metadata: ChunkMetadata
    tables: tuple[ExtractedTable, ...] = ()


@dataclass(frozen=True)
class SearchHit:
    """A retrieved chunk paired with its relevance score."""

    chunk: DocumentChunk
    score: float
