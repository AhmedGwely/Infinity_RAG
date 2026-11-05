"""Tests for the upcoming page-aware chunking milestone."""

import pytest

from app.core.schemas import ContentBlock, ExtractedTable, PageText
from app.ingestion.chunker import SemanticChunker


def test_chunker_rejects_invalid_overlap() -> None:
    """Reject configurations that could make chunk iteration invalid."""
    with pytest.raises(ValueError):
        SemanticChunker(chunk_size=100, overlap=100)


def test_chunker_preserves_page_metadata() -> None:
    """Verify chunks cite every physical and reliably detected printed page."""
    pages = [
        PageText(pdf_page=1, text="EDMI register information"),
        PageText(pdf_page=2, text="Protocol details", printed_page=2),
    ]

    chunks = SemanticChunker(chunk_size=200, overlap=20).chunk_pages(
        pages,
        document_id="doc-1",
        document_name="manual",
        document_type="pdf",
        source_file="manual.pdf",
    )

    assert len(chunks) == 1
    assert chunks[0].metadata.page_number == 1
    assert [(page.pdf_page, page.printed_page) for page in chunks[0].metadata.source_pages] == [
        (1, None),
        (2, 2),
    ]


def test_chunker_does_not_mix_sections() -> None:
    """Flush a chunk when the source heading changes."""
    pages = [
        PageText(
            pdf_page=1,
            text="",
            blocks=(
                ContentBlock("heading", "1 Meter", 1, "1", "1 Meter"),
                ContentBlock("paragraph", "Register map", 1, "1", "1 Meter"),
                ContentBlock("heading", "2 Communications", 1, "2", "2 Communications"),
                ContentBlock("paragraph", "Modbus setup", 1, "2", "2 Communications"),
            ),
        )
    ]

    chunks = SemanticChunker(chunk_size=500, overlap=20).chunk_pages(
        pages,
        document_id="doc-1",
        document_name="manual",
        document_type="pdf",
        source_file="manual.pdf",
    )

    assert len(chunks) == 2
    assert all(
        not ("Register map" in chunk.text and "Modbus setup" in chunk.text)
        for chunk in chunks
    )
    assert [chunk.metadata.section_number for chunk in chunks] == ["1", "2"]


def test_chunker_keeps_table_cells_and_headers_together() -> None:
    """Keep a table structured and attached to its source page when it exceeds the limit."""
    table = ExtractedTable(
        table_id="table-1",
        pdf_page=3,
        printed_page=2,
        caption="Register addresses",
        column_headers=("Address", "Description"),
        rows=(("0x1000", "Voltage"),),
        markdown="| Address | Description |\n| --- | --- |\n| 0x1000 | Voltage |",
    )
    page = PageText(
        pdf_page=3,
        text="Register table",
        printed_page=2,
        has_tables=True,
        tables=(table,),
        blocks=(ContentBlock("paragraph", "Register addresses", 3, "4", "Registers"),),
    )

    chunks = SemanticChunker(chunk_size=20, overlap=0).chunk_pages(
        [page],
        document_id="doc-1",
        document_name="manual",
        document_type="pdf",
        source_file="manual.pdf",
    )

    table_chunks = [chunk for chunk in chunks if chunk.tables]
    assert len(table_chunks) == 1
    assert table_chunks[0].tables[0].column_headers == ("Address", "Description")
    assert "0x1000" in table_chunks[0].text
    assert table_chunks[0].metadata.source_pages[0].pdf_page == 3
