"""PDF ingestion orchestration, JSON output, and extraction quality reporting."""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from app.core.config import Settings, load_settings
from app.core.exceptions import DocumentProcessingError
from app.core.logging_config import configure_logging
from app.core.schemas import (
    DocumentChunk,
    ExtractionIssue,
    PDFDocument,
)
from app.ingestion.chunker import SemanticChunker
from app.ingestion.document_parser import DoclingDocumentParser, normalize_document
from app.ingestion.pdf_loader import PDFLoader, PyMuPDFLoader

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QualityReport:
    """Counts and warnings that make extraction gaps easy to inspect."""

    pdf_filename: str
    pdf_page_count: int
    successfully_processed_pages: int
    empty_pages: int
    pages_requiring_review: int
    tables_detected: int
    sections_detected: int
    chunks_generated: int
    extraction_methods: tuple[str, ...]
    extraction_errors: tuple[str, ...]
    extraction_warnings: tuple[str, ...]


@dataclass(frozen=True)
class IngestionResult:
    """In-memory result and output locations for one processed PDF."""

    document: PDFDocument
    chunks: tuple[DocumentChunk, ...]
    report: QualityReport
    output_path: Path
    report_path: Path


@dataclass(frozen=True)
class BatchIngestionResult:
    """Successful PDF results plus file-level issues from a batch run."""

    results: tuple[IngestionResult, ...]
    issues: tuple[ExtractionIssue, ...]


class IngestionPipeline:
    """Coordinate PDF loading, structured parsing, chunking, and JSON persistence."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        loader: PDFLoader | None = None,
        parser: DoclingDocumentParser | None = None,
        chunker: SemanticChunker | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.loader = loader or PyMuPDFLoader()
        self.parser = parser or DoclingDocumentParser(offline=self.settings.docling_offline)
        self.chunker = chunker or SemanticChunker(
            chunk_size=self.settings.ingestion_chunk_size,
            overlap=self.settings.ingestion_chunk_overlap,
        )

    def ingest(self, source: str | Path, *, output_dir: str | Path | None = None) -> IngestionResult:
        """Process one PDF and write structured content and a quality report."""
        document = self.loader.load(source)
        return self._process_document(document, output_dir=output_dir)

    def ingest_batch(
        self,
        directory: str | Path | None = None,
        *,
        output_dir: str | Path | None = None,
    ) -> BatchIngestionResult:
        """Process every PDF in the incoming directory, preserving per-file failures."""
        source_dir = Path(directory) if directory is not None else self.settings.incoming_documents_dir
        batch = self.loader.load_batch(source_dir)
        results: list[IngestionResult] = []
        issues = list(batch.issues)
        for document in batch.documents:
            try:
                results.append(self._process_document(document, output_dir=output_dir))
            except Exception as error:
                logger.exception("Ingestion failed for %s", document.document_name)
                issues.append(
                    ExtractionIssue(
                        stage="ingestion_pipeline",
                        message=f"{document.document_name}: {error}",
                        severity="error",
                    )
                )
        return BatchIngestionResult(results=tuple(results), issues=tuple(issues))

    def _process_document(
        self,
        document: PDFDocument,
        *,
        output_dir: str | Path | None,
    ) -> IngestionResult:
        """Run parse, normalize, chunk, report, and atomic JSON writes."""
        if not document.pages or len(document.pages) != document.page_count:
            raise DocumentProcessingError(
                f"Page accounting failed for {document.document_name}: "
                f"expected {document.page_count}, got {len(document.pages)}"
            )

        parsed_document = normalize_document(self.parser.parse(document))
        chunks = tuple(
            self.chunker.chunk_pages(
                list(parsed_document.pages),
                document_id=parsed_document.document_id,
                document_name=parsed_document.document_name,
                document_type="pdf",
                source_file=str(parsed_document.source_file),
                document_version=parsed_document.document_version,
                ingestion_date=parsed_document.ingestion_date,
            )
        )
        report = build_quality_report(parsed_document, chunks)
        target_directory = Path(output_dir) if output_dir is not None else self.settings.processed_documents_dir
        target_directory.mkdir(parents=True, exist_ok=True)
        output_path = target_directory / f"{parsed_document.document_id}.json"
        report_path = target_directory / f"{parsed_document.document_id}.quality.json"

        _write_json(
            output_path,
            {
                "document": _to_jsonable(parsed_document),
                "chunks": _to_jsonable(chunks),
                "quality_report": _to_jsonable(report),
            },
        )
        _write_json(report_path, _to_jsonable(report))
        return IngestionResult(
            document=parsed_document,
            chunks=chunks,
            report=report,
            output_path=output_path,
            report_path=report_path,
        )


def build_quality_report(
    document: PDFDocument,
    chunks: tuple[DocumentChunk, ...] | list[DocumentChunk],
) -> QualityReport:
    """Summarize page accounting, extraction methods, tables, sections, and issues."""
    pages = document.pages
    section_keys = {
        (page.section_number, page.section_title)
        for page in pages
        if page.section_number or page.section_title
    }
    for page in pages:
        section_keys.update(
            (block.section_number, block.section_title)
            for block in page.blocks
            if block.content_type == "heading" and (block.section_number or block.section_title)
        )

    errors = tuple(issue.message for issue in document.issues if issue.severity == "error")
    warnings = tuple(issue.message for issue in document.issues if issue.severity != "error")
    return QualityReport(
        pdf_filename=document.source_file.name,
        pdf_page_count=document.page_count,
        successfully_processed_pages=sum(
            page.extraction_error is None and page.extraction_method != "pymupdf_failed"
            for page in pages
        ),
        empty_pages=sum(not page.text.strip() and not page.tables for page in pages),
        pages_requiring_review=sum(page.needs_review for page in pages),
        tables_detected=sum(len(page.tables) for page in pages),
        sections_detected=len(section_keys),
        chunks_generated=len(chunks),
        extraction_methods=tuple(sorted({page.extraction_method for page in pages})),
        extraction_errors=errors,
        extraction_warnings=warnings,
    )


def _to_jsonable(value):
    """Convert dataclasses and filesystem/time values into JSON-compatible values."""
    if hasattr(value, "__dataclass_fields__"):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _write_json(path: Path, payload) -> None:
    """Write UTF-8 JSON atomically so an interrupted run leaves no partial output."""
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main() -> int:
    """CLI entry point for one-PDF or incoming-directory ingestion."""
    argument_parser = argparse.ArgumentParser(description="Ingest local engineering PDFs.")
    argument_parser.add_argument("--input", type=Path, help="PDF file or directory of PDFs.")
    argument_parser.add_argument("--output", type=Path, help="Processed JSON output directory.")
    arguments = argument_parser.parse_args()
    settings = load_settings()
    configure_logging(settings.log_level)
    pipeline = IngestionPipeline(settings)

    if arguments.input and arguments.input.is_file():
        try:
            result = pipeline.ingest(arguments.input, output_dir=arguments.output)
            logger.info("Ingested %s; quality report: %s", result.report.pdf_filename, result.report_path)
            return 0
        except Exception:
            logger.exception("PDF ingestion failed for %s", arguments.input)
            return 1

    directory = arguments.input or settings.incoming_documents_dir
    try:
        batch = pipeline.ingest_batch(directory, output_dir=arguments.output)
    except Exception:
        logger.exception("Batch ingestion failed for %s", directory)
        return 1
    for result in batch.results:
        logger.info("Ingested %s; quality report: %s", result.report.pdf_filename, result.report_path)
    for issue in batch.issues:
        logger.error("%s", issue.message)
    return 1 if batch.issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
