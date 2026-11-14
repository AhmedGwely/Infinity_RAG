"""Format-independent chunk containers and a paragraph-aware text splitter."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT_SIZE = 1600
DEFAULT_OVERLAP = 200


@dataclass
class ParsedChunk:
    text: str
    locator: dict
    section: str | None = None


@dataclass
class ParsedDocument:
    title: str
    doc_type: str
    chunks: list[ParsedChunk] = field(default_factory=list)
    page_count: int | None = None
    issues: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def split_text(text: str, size: int = DEFAULT_SIZE, overlap: int = DEFAULT_OVERLAP) -> list[str]:
    """Split on paragraph boundaries, hard-splitting only oversized paragraphs."""
    if size <= overlap:
        raise ValueError("size must exceed overlap")
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= size:
            pieces.append(paragraph)
            continue
        start = 0
        while start < len(paragraph):
            end = min(start + size, len(paragraph))
            if end < len(paragraph):
                cut = max(paragraph.rfind(". ", start, end), paragraph.rfind("\n", start, end), paragraph.rfind(" ", start, end))
                if cut > start + size // 2:
                    end = cut + 1
            pieces.append(paragraph[start:end].strip())
            if end >= len(paragraph):
                break
            start = max(end - overlap, start + 1)
    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        candidate = f"{buffer}\n\n{piece}" if buffer else piece
        if len(candidate) <= size:
            buffer = candidate
        else:
            if buffer:
                chunks.append(buffer)
            buffer = piece
    if buffer:
        chunks.append(buffer)
    return [c for c in chunks if c.strip()]


def locator_label(locator: dict | None) -> str:
    """Human-readable location of a passage inside its source."""
    if not locator:
        return "location unavailable"
    kind = locator.get("type")
    if kind == "pdf":
        pdf = locator.get("pdf_pages") or []
        printed = locator.get("printed_pages") or []
        label = "PDF page" + ("s " if len(pdf) > 1 else " ") + ", ".join(map(str, pdf)) if pdf else "PDF page unavailable"
        if printed:
            label += " (printed " + ", ".join(map(str, printed)) + ")"
        return label
    if kind == "xlsx":
        return f"Sheet '{locator.get('sheet')}', rows {locator.get('row_start')}\u2013{locator.get('row_end')}"
    if kind == "csv":
        return f"Rows {locator.get('row_start')}\u2013{locator.get('row_end')}"
    if kind == "pptx":
        return f"Slide {locator.get('slide')}"
    if kind == "docx":
        return f"Section: {locator.get('section') or 'document body'}"
    if kind == "web":
        return f"Web page {locator.get('url', '')}".strip()
    if kind == "api":
        return f"API records {locator.get('record_start', '')}\u2013{locator.get('record_end', '')}"
    return f"Part {locator.get('part', 1)}"
