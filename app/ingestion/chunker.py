"""Configurable section-aware chunking that preserves page and table sources."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from app.core.schemas import (
    ChunkMetadata,
    ContentBlock,
    DocumentChunk,
    ExtractedTable,
    PageReference,
    PageText,
)


@dataclass(frozen=True)
class _ChunkUnit:
    """One structural item eligible for inclusion in a chunk."""

    text: str
    pdf_page: int
    printed_page: int | None
    section_number: str | None
    section_title: str | None
    table: ExtractedTable | None = None


class SemanticChunker:
    """Create bounded chunks while preserving engineering document metadata."""

    def __init__(self, chunk_size: int = 800, overlap: int = 120) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")
        if overlap < 0 or overlap >= chunk_size:
            raise ValueError("overlap must be non-negative and smaller than chunk_size")
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk_pages(
        self,
        pages: list[PageText],
        *,
        document_id: str,
        document_name: str,
        document_type: str,
        source_file: str,
        document_version: str | None = None,
        ingestion_date: datetime | None = None,
    ) -> list[DocumentChunk]:
        """Chunk related content without crossing section boundaries or splitting tables."""
        timestamp = ingestion_date or datetime.now(timezone.utc)
        units = self._build_units(pages)
        chunks: list[DocumentChunk] = []
        buffer: list[_ChunkUnit] = []
        buffer_is_overlap = False
        current_section: tuple[str | None, str | None] | None = None

        def flush(*, preserve_overlap: bool = True) -> None:
            nonlocal buffer, buffer_is_overlap
            if not buffer:
                return
            if buffer_is_overlap:
                buffer = []
                buffer_is_overlap = False
                return
            chunks.append(
                self._create_chunk(
                    buffer,
                    chunk_index=len(chunks) + 1,
                    document_id=document_id,
                    document_name=document_name,
                    document_type=document_type,
                    document_version=document_version,
                    source_file=source_file,
                    ingestion_date=timestamp,
                )
            )
            if preserve_overlap and self.overlap:
                last_unit = next((unit for unit in reversed(buffer) if unit.table is None), None)
                if last_unit is not None:
                    overlap_text = last_unit.text[-self.overlap :].strip()
                    buffer = [
                        _ChunkUnit(
                            text=overlap_text,
                            pdf_page=last_unit.pdf_page,
                            printed_page=last_unit.printed_page,
                            section_number=last_unit.section_number,
                            section_title=last_unit.section_title,
                        )
                    ] if overlap_text else []
                else:
                    buffer = []
                buffer_is_overlap = bool(buffer)
            else:
                buffer = []
                buffer_is_overlap = False

        for unit in units:
            section = (unit.section_number, unit.section_title)
            if buffer and section != current_section:
                if buffer_is_overlap:
                    buffer = []
                    buffer_is_overlap = False
                else:
                    flush(preserve_overlap=False)
            current_section = section

            unit_parts = [unit] if unit.table is not None else self._split_prose_unit(unit)
            for part in unit_parts:
                buffered_length = sum(len(existing.text) for existing in buffer)
                if buffer and buffered_length + len(part.text) > self.chunk_size:
                    flush()
                    current_section = section
                buffer.append(part)
                buffer_is_overlap = False

        flush(preserve_overlap=False)
        return chunks

    def _build_units(self, pages: list[PageText]) -> list[_ChunkUnit]:
        """Convert ordered page blocks (or fallback text) into chunkable units."""
        units: list[_ChunkUnit] = []
        for page in pages:
            if page.blocks:
                included_table_ids: set[str] = set()
                for block in page.blocks:
                    table = self._find_table(page.tables, block.table_id) if block.table_id else None
                    text = block.text.strip()
                    if table is not None:
                        included_table_ids.add(table.table_id)
                        text = "\n".join(part for part in (table.caption, table.markdown) if part)
                    if text:
                        units.append(
                            _ChunkUnit(
                                text=text,
                                pdf_page=page.pdf_page,
                                printed_page=page.printed_page,
                                section_number=block.section_number or page.section_number,
                                section_title=block.section_title or page.section_title,
                                table=table,
                            )
                        )
                for table in page.tables:
                    if table.table_id in included_table_ids:
                        continue
                    table_text = "\n".join(part for part in (table.caption, table.markdown) if part)
                    if table_text:
                        units.append(
                            _ChunkUnit(
                                text=table_text,
                                pdf_page=table.pdf_page,
                                printed_page=table.printed_page,
                                section_number=table.section_number or page.section_number,
                                section_title=table.section_title or page.section_title,
                                table=table,
                            )
                        )
                continue

            for paragraph in re.split(r"\n\s*\n", page.text):
                text = paragraph.strip()
                if text:
                    units.append(
                        _ChunkUnit(
                            text=text,
                            pdf_page=page.pdf_page,
                            printed_page=page.printed_page,
                            section_number=page.section_number,
                            section_title=page.section_title,
                        )
                    )
            for table in page.tables:
                table_text = "\n".join(part for part in (table.caption, table.markdown) if part)
                if table_text:
                    units.append(
                        _ChunkUnit(
                            text=table_text,
                            pdf_page=table.pdf_page,
                            printed_page=table.printed_page,
                            section_number=table.section_number or page.section_number,
                            section_title=table.section_title or page.section_title,
                            table=table,
                        )
                    )
        return units

    def _split_prose_unit(self, unit: _ChunkUnit) -> list[_ChunkUnit]:
        """Split long prose on whitespace, retaining configured overlap."""
        if len(unit.text) <= self.chunk_size:
            return [unit]
        parts: list[_ChunkUnit] = []
        start = 0
        part_size = self.chunk_size - self.overlap
        while start < len(unit.text):
            end = min(start + part_size, len(unit.text))
            if end < len(unit.text):
                boundary = unit.text.rfind(" ", start, end)
                if boundary > start:
                    end = boundary
            part_text = unit.text[start:end].strip()
            if part_text:
                parts.append(
                    _ChunkUnit(
                        text=part_text,
                        pdf_page=unit.pdf_page,
                        printed_page=unit.printed_page,
                        section_number=unit.section_number,
                        section_title=unit.section_title,
                    )
                )
            if end >= len(unit.text):
                break
            start = max(end, start + 1)
        return parts

    @staticmethod
    def _find_table(tables: tuple[ExtractedTable, ...], table_id: str) -> ExtractedTable | None:
        return next((table for table in tables if table.table_id == table_id), None)

    @staticmethod
    def _create_chunk(
        units: list[_ChunkUnit],
        *,
        chunk_index: int,
        document_id: str,
        document_name: str,
        document_type: str,
        document_version: str | None,
        source_file: str,
        ingestion_date: datetime,
    ) -> DocumentChunk:
        text = "\n\n".join(unit.text for unit in units)
        source_pages = tuple(
            PageReference(pdf_page=page_number, printed_page=printed_page)
            for page_number, printed_page in _unique_pages(units)
        )
        first_unit = units[0]
        chunk_id = str(uuid5(NAMESPACE_URL, f"{document_id}:{chunk_index}:{text}"))
        table_by_id = {unit.table.table_id: unit.table for unit in units if unit.table is not None}
        metadata = ChunkMetadata(
            document_id=document_id,
            document_name=document_name,
            document_type=document_type,
            page_number=first_unit.pdf_page,
            section_title=first_unit.section_title,
            chunk_id=chunk_id,
            source_file=source_file,
            ingestion_date=ingestion_date,
            document_version=document_version,
            section_number=first_unit.section_number,
            source_pages=source_pages,
        )
        return DocumentChunk(text=text, metadata=metadata, tables=tuple(table_by_id.values()))


def _unique_pages(units: list[_ChunkUnit]) -> list[tuple[int, int | None]]:
    """Return source pages in first-appearance order, preserving printed numbers."""
    pages: dict[int, int | None] = {}
    for unit in units:
        pages.setdefault(unit.pdf_page, unit.printed_page)
    return list(pages.items())
