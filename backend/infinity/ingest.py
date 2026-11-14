"""Indexing: parsed chunks -> BGE-M3 vectors -> Qdrant points + PostgreSQL mirror (keyword search, citations)."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .chunking import ParsedDocument
from .embedder_client import EmbedderClient
from .models import Chunk, Document
from .parsers import parse_file
from .qdrant_gateway import QdrantGateway

log = logging.getLogger(__name__)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _payload(doc: Document, parsed: ParsedDocument, chunk_id: str, text: str, locator: dict, section: str | None, now: datetime) -> dict:
    pdf_pages = locator.get("pdf_pages") or []
    printed = locator.get("printed_pages") or []
    return {
        "text": text,
        # Same nested shape as the pre-existing collection so the legacy API can still read these points.
        "metadata": {
            "document_id": doc.id, "document_name": doc.title, "document_type": doc.doc_type,
            "page_number": int(pdf_pages[0]) if pdf_pages else 1, "section_title": section, "chunk_id": chunk_id,
            "source_file": doc.uri or doc.title, "ingestion_date": now.isoformat(), "document_version": parsed.metadata.get("version"),
            "section_number": None,
            "source_pages": [{"pdf_page": int(p), "printed_page": (int(printed[i]) if i < len(printed) else None)} for i, p in enumerate(pdf_pages)],
        },
        "tables": [],
        "locator": locator,
        "platform": {"source_id": doc.source_id, "kind": "platform"},
    }


def index_parsed(session: Session, doc: Document, parsed: ParsedDocument, qdrant: QdrantGateway, embedder: EmbedderClient) -> int:
    """Replace this document's chunks. Legacy (pre-existing) documents are never re-indexed."""
    if doc.legacy:
        raise RuntimeError("refusing to re-index a document mirrored from the pre-existing Qdrant collection")
    if not parsed.chunks:
        raise ValueError("; ".join(parsed.issues) or "no extractable text")
    now = datetime.now(timezone.utc)
    texts = [c.text for c in parsed.chunks]
    vectors = embedder.embed(texts)
    new_ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc.id}:{now.timestamp()}:{i}")) for i in range(len(texts))]
    points = [
        (cid, vec, _payload(doc, parsed, cid, c.text, c.locator, c.section, now))
        for cid, vec, c in zip(new_ids, vectors, parsed.chunks)
    ]
    qdrant.upsert(points)
    try:
        old_ids = [r[0] for r in session.execute(select(Chunk.id).where(Chunk.document_id == doc.id)).all()]
        session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        session.add_all(
            Chunk(id=cid, document_id=doc.id, ord=i, text=c.text, section_title=c.section, locator=c.locator)
            for i, (cid, c) in enumerate(zip(new_ids, parsed.chunks))
        )
        doc.chunk_count = len(new_ids)
        doc.page_count = parsed.page_count
        doc.issues = parsed.issues
        doc.status, doc.error = "ready", None
        doc.updated_at = now
        session.commit()
    except Exception:
        session.rollback()
        qdrant.delete_points(new_ids)  # compensate: remove only the points we just added
        raise
    if old_ids:
        qdrant.delete_points(old_ids)  # old, platform-owned points only (legacy docs are refused above)
    return len(new_ids)


def ingest_document(session: Session, doc_id: str, qdrant: QdrantGateway, embedder: EmbedderClient) -> int:
    doc = session.get(Document, doc_id)
    if doc is None:
        raise LookupError("document not found")
    doc.status = "processing"
    doc.error = None
    session.commit()
    try:
        parsed = parse_file(doc.file_path, (doc.meta or {}).get("filename"))
        doc.content_hash = sha256_file(doc.file_path)
        if parsed.title and not (doc.meta or {}).get("title_locked"):
            doc.title = parsed.title[:500]
        doc.doc_type = parsed.doc_type
        return index_parsed(session, doc, parsed, qdrant, embedder)
    except Exception as error:
        session.rollback()
        doc = session.get(Document, doc_id)
        doc.status, doc.error = "failed", f"{type(error).__name__}: {str(error)[:500]}"
        session.commit()
        raise


def delete_document(session: Session, doc: Document, qdrant: QdrantGateway) -> None:
    """Remove a platform-owned document, its points and its stored file. Legacy documents are protected."""
    if doc.legacy:
        raise PermissionError("documents mirrored from the pre-existing Qdrant collection cannot be deleted here")
    ids = [r[0] for r in session.execute(select(Chunk.id).where(Chunk.document_id == doc.id)).all()]
    qdrant.delete_points(ids)
    path = doc.file_path
    session.delete(doc)
    session.commit()
    if path:
        try:
            Path(path).unlink(missing_ok=True)
            Path(path).parent.rmdir()
        except OSError:
            pass
