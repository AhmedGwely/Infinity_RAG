from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_config
from ..deps import admin_only, can_view, current_user, engineer_up, get_db
from ..ingest import delete_document, sha256_file
from ..models import CLEARANCE, VISIBILITIES, Chunk, Document, Job, User
from ..parsers import SUPPORTED_SUFFIXES
from ..ratelimit import limit
from ..services import qdrant

router = APIRouter(tags=["documents"])
INLINE_TYPES = {".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8"}


def doc_dict(d: Document, detail: bool = False) -> dict:
    out = {
        "id": d.id, "title": d.title, "doc_type": d.doc_type, "status": d.status, "visibility": d.visibility, "error": d.error,
        "chunk_count": d.chunk_count, "page_count": d.page_count, "size_bytes": d.size_bytes, "legacy": d.legacy,
        "uri": d.uri if d.doc_type in {"web", "api"} or (d.uri or "").startswith("http") else None,
        "has_file": bool(d.file_path), "source_id": d.source_id, "source_name": d.source.name if d.source else None,
        "created_at": d.created_at, "updated_at": d.updated_at, "last_seen_at": d.last_seen_at,
    }
    if detail:
        out["issues"] = d.issues
        out["meta"] = {k: v for k, v in (d.meta or {}).items() if k not in {"etag", "last_modified"}}
        out["origin_path"] = d.uri if d.legacy else None
    return out


def _visible_or_404(db: Session, user: User, doc_id: str) -> Document:
    doc = db.get(Document, doc_id)
    if not doc or not can_view(user, doc):
        raise HTTPException(404, "Document not found.")
    return doc


@router.get("/documents")
def list_documents(q: str | None = None, status: str | None = None, doc_type: str | None = None, source_id: str | None = None,
                   limit_n: int = 200, user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(Document).where(Document.visibility.in_(sorted(CLEARANCE.get(user.role, {"public"}))))
    if q:
        stmt = stmt.where(Document.title.ilike(f"%{q}%"))
    if status:
        stmt = stmt.where(Document.status == status)
    if doc_type:
        stmt = stmt.where(Document.doc_type == doc_type)
    if source_id:
        stmt = stmt.where(Document.source_id == source_id)
    rows = db.scalars(stmt.order_by(Document.updated_at.desc()).limit(min(limit_n, 500))).all()
    return [doc_dict(d) for d in rows]


@router.post("/documents/upload", status_code=201)
async def upload(request: Request, files: list[UploadFile] = File(...), visibility: str = Form("internal"),
                 user: User = Depends(engineer_up), db: Session = Depends(get_db)):
    if visibility not in VISIBILITIES:
        raise HTTPException(422, "Invalid visibility.")
    if visibility not in CLEARANCE[user.role]:
        raise HTTPException(403, "You cannot upload documents above your own clearance.")
    limit(f"upload:{user.id}", 30, 60)
    cfg = get_config()
    created, rejected = [], []
    for file in files:
        name = re.sub(r"[^\w.\- ()\u0600-\u06FF]+", "_", Path(file.filename or "upload").name)[:200] or "upload"
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            rejected.append({"filename": name, "reason": f"unsupported type {suffix or '(none)'}"})
            continue
        doc_id = str(uuid.uuid4())
        folder = Path(cfg.upload_dir) / doc_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        size = 0
        too_big = False
        with open(path, "wb") as out:
            while block := await file.read(1 << 20):
                size += len(block)
                if size > cfg.max_upload_mb * 1024 * 1024:
                    too_big = True
                    break
                out.write(block)
        if too_big or size == 0:
            path.unlink(missing_ok=True); folder.rmdir()
            rejected.append({"filename": name, "reason": f"larger than {cfg.max_upload_mb} MB" if too_big else "file is empty"})
            continue
        digest = sha256_file(path)
        duplicate = db.scalar(select(Document).where(Document.content_hash == digest, Document.status != "failed", Document.legacy.is_(False)))
        if duplicate:
            path.unlink(missing_ok=True); folder.rmdir()
            rejected.append({"filename": name, "reason": f"identical file already indexed as \u201c{duplicate.title}\u201d"})
            continue
        doc = Document(id=doc_id, title=Path(name).stem, doc_type=suffix.lstrip("."), file_path=str(path), content_hash=digest, visibility=visibility,
                       status="queued", size_bytes=size, meta={"filename": name}, uploaded_by=user.id)
        db.add(doc)
        db.add(Job(kind="ingest_file", ref_id=doc_id, label=f"Index {name}", created_by=user.id))
        db.commit()
        created.append(doc_dict(doc))
    audit.record(db, "documents_uploaded", request=request, user=user, detail={"accepted": [d["title"] for d in created], "rejected": rejected, "visibility": visibility})
    return {"created": created, "rejected": rejected}


@router.get("/documents/{doc_id}")
def get_document(doc_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return doc_dict(_visible_or_404(db, user, doc_id), detail=True)


@router.get("/documents/{doc_id}/file")
def get_file(doc_id: str, request: Request, download: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _visible_or_404(db, user, doc_id)
    if not doc.file_path or not Path(doc.file_path).is_file():
        raise HTTPException(404, "The original file is not stored on the server.")
    suffix = Path(doc.file_path).suffix.lower()
    inline = suffix in INLINE_TYPES and not download
    audit.record(db, "document_file_opened", request=request, user=user, target=doc.title)
    return FileResponse(
        doc.file_path, media_type=INLINE_TYPES.get(suffix, "application/octet-stream"), filename=Path(doc.file_path).name,
        content_disposition_type="inline" if inline else "attachment", headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/chunks/{chunk_id}")
def get_chunk(chunk_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """A cited passage with its neighbours, so a citation can be opened and checked in context."""
    chunk = db.get(Chunk, chunk_id)
    if not chunk:
        raise HTTPException(404, "Passage not found.")
    doc = _visible_or_404(db, user, chunk.document_id)
    neighbours = db.scalars(
        select(Chunk).where(Chunk.document_id == doc.id, Chunk.ord.between(chunk.ord - 1, chunk.ord + 1)).order_by(Chunk.ord, Chunk.id).limit(6)
    ).all()
    from ..chunking import locator_label
    return {
        "chunk": {"id": chunk.id, "text": chunk.text, "section": chunk.section_title, "locator": chunk.locator, "location": locator_label(chunk.locator)},
        "context": [{"id": n.id, "text": n.text, "location": locator_label(n.locator), "is_cited": n.id == chunk.id} for n in neighbours],
        "document": doc_dict(doc),
    }


class DocPatch(BaseModel):
    title: str | None = None
    visibility: str | None = None


def _owner_or_admin(user: User, doc: Document) -> None:
    if user.role != "admin" and doc.uploaded_by != user.id:
        raise HTTPException(403, "Only an administrator or the uploader can change this document.")


@router.patch("/documents/{doc_id}")
def patch_document(doc_id: str, body: DocPatch, request: Request, user: User = Depends(engineer_up), db: Session = Depends(get_db)):
    doc = _visible_or_404(db, user, doc_id)
    _owner_or_admin(user, doc)
    if body.title:
        doc.title = body.title.strip()[:500]
        doc.meta = {**(doc.meta or {}), "title_locked": True}
    if body.visibility:
        if body.visibility not in VISIBILITIES or body.visibility not in CLEARANCE[user.role]:
            raise HTTPException(422, "Invalid visibility for your clearance.")
        doc.visibility = body.visibility  # enforced at query time from PostgreSQL; no re-indexing needed
    db.commit()
    audit.record(db, "document_updated", request=request, user=user, target=doc.title, detail=body.model_dump(exclude_none=True))
    return doc_dict(doc)


@router.delete("/documents/{doc_id}")
def remove_document(doc_id: str, request: Request, user: User = Depends(engineer_up), db: Session = Depends(get_db)):
    doc = _visible_or_404(db, user, doc_id)
    _owner_or_admin(user, doc)
    title = doc.title
    try:
        delete_document(db, doc, qdrant())
    except PermissionError as error:
        raise HTTPException(403, str(error)) from None
    audit.record(db, "document_deleted", request=request, user=user, target=title)
    return {"ok": True}


@router.post("/documents/{doc_id}/reindex")
def reindex(doc_id: str, request: Request, user: User = Depends(engineer_up), db: Session = Depends(get_db)):
    doc = _visible_or_404(db, user, doc_id)
    _owner_or_admin(user, doc)
    if doc.legacy:
        raise HTTPException(400, "Documents from the pre-existing index are never re-indexed automatically.")
    if not doc.file_path:
        raise HTTPException(400, "This document has no stored file. Re-run the source sync instead.")
    doc.status = "queued"
    db.add(Job(kind="ingest_file", ref_id=doc.id, label=f"Re-index {doc.title}", created_by=user.id))
    db.commit()
    audit.record(db, "document_reindex", request=request, user=user, target=doc.title)
    return doc_dict(doc)


@router.post("/documents/{doc_id}/attach-file")
async def attach_file(doc_id: str, request: Request, file: UploadFile = File(...), user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Attach the original file to a pre-existing (legacy) document so citations can open it. Does NOT re-index."""
    doc = db.get(Document, doc_id)
    if not doc or not doc.legacy:
        raise HTTPException(404, "Only documents from the pre-existing index can have a file attached.")
    cfg = get_config()
    folder = Path(cfg.upload_dir) / doc.id
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.\-]+", "_", Path(file.filename or "original.pdf").name)
    path = folder / name
    size = 0
    with open(path, "wb") as out:
        while block := await file.read(1 << 20):
            size += len(block)
            if size > cfg.max_upload_mb * 1024 * 1024:
                out.close(); path.unlink(missing_ok=True)
                raise HTTPException(413, f"File is larger than {cfg.max_upload_mb} MB.")
            out.write(block)
    doc.file_path, doc.size_bytes = str(path), size
    db.commit()
    audit.record(db, "legacy_file_attached", request=request, user=user, target=doc.title)
    return doc_dict(doc)
