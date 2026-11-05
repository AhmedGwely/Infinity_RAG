"""PDF ingestion tests using generated PDFs and a conditional EDMI integration test."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.exceptions import PDFValidationError, PasswordProtectedPDFError
from app.core.schemas import ExtractionIssue, PDFDocument, PageText
from app.ingestion.document_parser import DoclingDocumentParser, normalize_page
from app.ingestion.ingestion_pipeline import build_quality_report
from app.ingestion.pdf_loader import (
    PyMuPDFLoader,
    _remove_degenerate_empty_columns,
    detect_printed_page,
    table_to_markdown,
)


@pytest.fixture
def fitz_module():
    """Import PyMuPDF only for tests that create or read PDFs."""
    return pytest.importorskip("fitz", reason="PyMuPDF is not installed")


def _create_pdf(path: Path, page_count: int = 1):
    """Create a tiny deterministic PDF fixture."""
    fitz = pytest.importorskip("fitz", reason="PyMuPDF is not installed")
    pdf = fitz.open()
    for _ in range(page_count):
        pdf.new_page()
    pdf.save(path)
    pdf.close()
    return fitz


def test_loader_rejects_missing_and_empty_files(tmp_path: Path) -> None:
    """Fail early with a clear error before attempting PDF parsing."""
    loader = PyMuPDFLoader()
    with pytest.raises(PDFValidationError, match="does not exist"):
        loader.load(tmp_path / "missing.pdf")

    empty_pdf = tmp_path / "empty.pdf"
    empty_pdf.write_bytes(b"")
    with pytest.raises(PDFValidationError, match="empty"):
        loader.load(empty_pdf)


def test_loader_rejects_non_pdf_extension(tmp_path: Path) -> None:
    """Reject non-PDF paths explicitly."""
    text_file = tmp_path / "manual.txt"
    text_file.write_text("not a PDF", encoding="utf-8")
    with pytest.raises(PDFValidationError, match="Expected a .pdf"):
        PyMuPDFLoader().load(text_file)


def test_loader_accounts_for_every_physical_page(tmp_path: Path, fitz_module) -> None:
    """Extract physical page numbers as 1-indexed values, including blank pages."""
    source = tmp_path / "Mk6 manual v1.3.pdf"
    pdf = fitz_module.open()
    first = pdf.new_page()
    first.insert_text((72, 72), "Register address 0x1A2B: 230 V")
    pdf.new_page()
    pdf.save(source)
    pdf.close()

    document = PyMuPDFLoader().load(source)

    assert document.page_count == 2
    assert [page.pdf_page for page in document.pages] == [1, 2]
    assert document.document_version == "1.3"
    assert "0x1A2B" in document.pages[0].text
    assert document.pages[1].needs_review
    assert document.pages[1].content_type == "empty"


def test_loader_handles_corrupted_pdf(tmp_path: Path, fitz_module) -> None:
    """Report unreadable PDF data as a validation error."""
    source = tmp_path / "corrupt.pdf"
    source.write_bytes(b"%PDF-1.7\nthis is not a valid PDF")
    with pytest.raises(PDFValidationError, match="unreadable or corrupted"):
        PyMuPDFLoader().load(source)


def test_loader_handles_password_protected_pdf(tmp_path: Path, fitz_module) -> None:
    """Do not prompt or silently treat an encrypted manual as empty."""
    source = tmp_path / "protected.pdf"
    pdf = fitz_module.open()
    pdf.new_page()
    pdf.save(source, encryption=fitz_module.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    pdf.close()

    with pytest.raises(PasswordProtectedPDFError, match="password-protected"):
        PyMuPDFLoader().load(source)


def test_printed_page_detection_requires_explicit_footer_label() -> None:
    """Do not mistake an unlabeled footer numeral for a printed page number."""
    assert detect_printed_page("Manual content\nPage 12 of 179") == 12
    assert detect_printed_page("Manual content\n12") is None
    assert detect_printed_page("Page 12\nPage 13") is None


def test_normalization_preserves_engineering_notation() -> None:
    """Normalize whitespace without rewriting register addresses, symbols, or units."""
    page = PageText(
        pdf_page=4,
        text="Register\t0x1A2B   =  230 V\n\n\nPhase angle: 30°",
        extraction_method="pymupdf",
    )

    normalized = normalize_page(page)

    assert normalized.pdf_page == 4
    assert "0x1A2B" in normalized.text
    assert "230 V" in normalized.text
    assert "30°" in normalized.text
    assert "\n\n\n" not in normalized.text


def test_table_markdown_retains_headers_and_cells() -> None:
    """Check Markdown rendering does not drop technical header or cell text."""
    markdown = table_to_markdown(
        ("Register", "Address", "Units"),
        (("Voltage L1", "0x1A2B", "230 V"),),
    )

    assert "| Register | Address | Units |" in markdown
    assert "0x1A2B" in markdown
    assert "230 V" in markdown


def test_pymupdf_table_extraction_keeps_headers_and_rows(tmp_path: Path, fitz_module) -> None:
    """Extract a ruled test table as headers and structured rows."""
    source = tmp_path / "table.pdf"
    pdf = fitz_module.open()
    page = pdf.new_page()
    x_positions = (72, 250, 450)
    y_positions = (100, 130, 160)
    for y_position in y_positions:
        page.draw_line((x_positions[0], y_position), (x_positions[-1], y_position), width=1)
    for x_position in x_positions:
        page.draw_line((x_position, y_positions[0]), (x_position, y_positions[-1]), width=1)
    page.insert_text((78, 120), "Register")
    page.insert_text((256, 120), "Units")
    page.insert_text((78, 150), "0x1A2B")
    page.insert_text((256, 150), "230 V")
    pdf.save(source)
    pdf.close()

    document = PyMuPDFLoader().load(source)

    assert document.pages[0].has_tables
    table = document.pages[0].tables[0]
    assert table.column_headers == ("Register", "Units")
    assert ("0x1A2B", "230 V") in table.rows
    assert "0x1A2B" in table.markdown


def test_table_normalization_preserves_wide_empty_columns() -> None:
    """Keep empty columns when their measured geometry is not degenerate."""
    table = SimpleNamespace(
        col_count=3,
        row_count=3,
        cells=[
            (0, 0, 40, 10), (40, 0, 70, 10), (70, 0, 110, 10),
            (0, 10, 40, 20), (40, 10, 70, 20), (70, 10, 110, 20),
            (0, 20, 40, 30), (40, 20, 70, 30), (70, 20, 110, 30),
        ],
    )
    headers = ("Register", "", "Units")
    rows = (("0x1000", "", "230 V"), ("0x1001", "", "231 V"))

    normalized_headers, normalized_rows = _remove_degenerate_empty_columns(table, headers, rows)

    assert normalized_headers == headers
    assert normalized_rows == rows


def test_table_normalization_preserves_unknown_merged_cells() -> None:
    """Keep empty columns if merged-cell geometry prevents proving degeneracy."""
    table = SimpleNamespace(
        col_count=3,
        row_count=2,
        cells=[(0, 0, 40, 10), None, (42, 0, 80, 10), (0, 10, 40, 20), (40, 10, 42, 20), (42, 10, 80, 20)],
    )
    headers = ("Register", "", "Units")
    rows = (("0x1000", "", "230 V"),)

    normalized_headers, normalized_rows = _remove_degenerate_empty_columns(table, headers, rows)

    assert normalized_headers == headers
    assert normalized_rows == rows


def test_docling_fallback_is_explicit_and_keeps_all_pages() -> None:
    """A structured-parser fallback preserves physical page records and reports why."""
    document = PDFDocument(
        document_id="doc-id",
        document_name="manual",
        document_version="1.3",
        source_file=Path("manual.pdf"),
        page_count=2,
        file_metadata={},
        ingestion_date=datetime.now(timezone.utc),
        pages=(PageText(pdf_page=1, text="Page one"), PageText(pdf_page=2, text="Page two")),
    )

    fallback = DoclingDocumentParser._fallback(document, "Docling unavailable in offline mode")

    assert [page.pdf_page for page in fallback.pages] == [1, 2]
    assert all(page.extraction_method == "pymupdf_fallback" for page in fallback.pages)
    assert all(page.needs_review for page in fallback.pages)
    assert fallback.issues[0].stage == "docling_fallback"


def test_quality_report_counts_pages_sections_tables_and_errors() -> None:
    """Summarize extraction problems instead of hiding them in page records."""
    document = PDFDocument(
        document_id="doc-id",
        document_name="manual",
        document_version=None,
        source_file=Path("manual.pdf"),
        page_count=2,
        file_metadata={},
        ingestion_date=datetime.now(timezone.utc),
        pages=(
            PageText(pdf_page=1, text="Heading", section_number="1", section_title="Overview"),
            PageText(pdf_page=2, text="", content_type="empty", needs_review=True),
        ),
        issues=(ExtractionIssue("test", "page parse warning"),),
    )

    report = build_quality_report(document, [])

    assert report.pdf_page_count == 2
    assert report.successfully_processed_pages == 2
    assert report.empty_pages == 1
    assert report.pages_requiring_review == 1
    assert report.sections_detected == 1
    assert report.extraction_warnings == ("page parse warning",)


def test_edmi_manual_integration(tmp_path: Path) -> None:
    """Account for all 179 manual pages when the document and parser dependencies are available."""
    manual = Path(r"D:\Manuals\Mk6Es-Mk6ESL User Manual v1.3.pdf")
    if not manual.is_file():
        pytest.skip("EDMI manual was not found at D:\\Manuals")
    if importlib.util.find_spec("fitz") is None:
        pytest.skip("PyMuPDF is not installed")
    if importlib.util.find_spec("docling") is None:
        pytest.skip("Docling is not installed")

    from app.ingestion.ingestion_pipeline import IngestionPipeline

    result = IngestionPipeline().ingest(manual, output_dir=tmp_path)

    assert result.document.page_count == 179
    assert [page.pdf_page for page in result.document.pages] == list(range(1, 180))
    assert result.report.successfully_processed_pages == 179
    assert result.report.pdf_filename == manual.name
    assert result.output_path.is_file()
    assert result.report_path.is_file()
    assert all(chunk.metadata.source_pages for chunk in result.chunks)
