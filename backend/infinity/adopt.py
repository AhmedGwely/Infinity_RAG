"""Mirror the pre-existing Qdrant collection into PostgreSQL (READ-ONLY with respect to Qdrant).

No vectors are read, written or recomputed and no point is modified; only payload text/metadata is copied so the
platform can do keyword search, enforce visibility and resolve citations for the existing 631 points.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .config import get_config
from .models import Chunk, Document
from .qdrant_gateway import QdrantGateway


def legacy_document_uuid(legacy_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"infinity-legacy:{legacy_id}"))


def adopt_legacy(session: Session, qdrant: QdrantGateway, progress: Callable[[str], None] | None = None) -> dict:
    cfg = get_config()
    total = qdrant.count()
    seen = inserted_chunks = skipped = 0
    docs_touched: set[str] = set()
    now = datetime.now(timezone.utc)
    for batch in qdrant.scroll_payloads():
        for point in batch:
            seen += 1
            payload = point.payload or {}
            meta = payload.get("metadata") or {}
            text = payload.get("text")
            chunk_id = str(point.id)
            legacy_doc = meta.get("document_id")
            if payload.get("platform") or not text or not legacy_doc or len(chunk_id) > 36:
                skipped += 1  # platform-created points and unusable payloads are not "legacy"
                continue
            doc_id = legacy_document_uuid(str(legacy_doc))
            if doc_id not in docs_touched:
                session.execute(
                    insert(Document).values(
                        id=doc_id, title=str(meta.get("document_name") or "Untitled")[:500], doc_type=str(meta.get("document_type") or "pdf")[:20],
                        uri=meta.get("source_file"), visibility=cfg.legacy_visibility, status="ready", legacy=True,
                        meta={"legacy_document_id": legacy_doc, "version": meta.get("document_version"), "origin": "pre-existing Qdrant collection"},
                        issues=[], created_at=now, updated_at=now, last_seen_at=now,
                    ).on_conflict_do_nothing(index_elements=["id"])
                )
                docs_touched.add(doc_id)
            pages = meta.get("source_pages") or []
            pdf_pages = list(dict.fromkeys(p["pdf_page"] for p in pages if isinstance(p, dict) and p.get("pdf_page")))
            printed = list(dict.fromkeys(p["printed_page"] for p in pages if isinstance(p, dict) and p.get("printed_page")))
            if not pdf_pages and isinstance(meta.get("page_number"), int):
                pdf_pages = [meta["page_number"]]
            result = session.execute(
                insert(Chunk).values(
                    id=chunk_id, document_id=doc_id, ord=(pdf_pages[0] if pdf_pages else 0), text=text,
                    section_title=" ".join(x for x in [meta.get("section_number"), meta.get("section_title")] if x) or None,
                    locator={"type": "pdf", "pdf_pages": pdf_pages, "printed_pages": printed},
                ).on_conflict_do_nothing(index_elements=["id"])
            )
            inserted_chunks += result.rowcount or 0
        session.commit()
        if progress:
            progress(f"mirrored {seen}/{total} points")
    for doc_id in docs_touched:
        count = session.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == doc_id))
        session.execute(update(Document).where(Document.id == doc_id).values(chunk_count=count))
    session.commit()
    return {"qdrant_points": total, "points_seen": seen, "chunks_added": inserted_chunks, "skipped": skipped, "documents": len(docs_touched)}
