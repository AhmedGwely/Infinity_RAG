from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import apiconnector, audit, crawler
from ..config import get_config
from ..deps import admin_only, get_db
from ..fetcher import SafeFetcher
from ..models import VISIBILITIES, Document, Job, Source, User
from ..netguard import UnsafeURL, assert_safe_url
from ..services import qdrant, secret_box

router = APIRouter(prefix="/sources", tags=["sources"])


class SourceBody(BaseModel):
    name: str
    kind: str
    config: dict
    visibility: str = "internal"
    enabled: bool = True
    schedule_minutes: int | None = None
    secret: str | None = None  # API token / key; stored encrypted, never returned


class SourcePatch(BaseModel):
    name: str | None = None
    config: dict | None = None
    visibility: str | None = None
    enabled: bool | None = None
    schedule_minutes: int | None = None
    clear_schedule: bool = False
    secret: str | None = None
    clear_secret: bool = False


def source_dict(db: Session, s: Source) -> dict:
    counts = dict(db.execute(select(Document.status, func.count()).where(Document.source_id == s.id).group_by(Document.status)).all())
    return {
        "id": s.id, "name": s.name, "kind": s.kind, "config": s.config, "visibility": s.visibility, "enabled": s.enabled,
        "schedule_minutes": s.schedule_minutes, "status": s.status, "last_sync_at": s.last_sync_at, "last_error": s.last_error,
        "has_secret": bool(s.secret_enc), "documents": counts, "created_at": s.created_at,
    }


def _normalise(kind: str, config: dict) -> dict:
    cfg = get_config()
    try:
        if kind == "website":
            clean = crawler.validate_config(config)
            for url in clean["start_urls"]:
                assert_safe_url(url, allow_private=cfg.allow_private_sources)
            return clean
        if kind == "api":
            clean = apiconnector.validate_config(config)
            assert_safe_url(clean["url"], allow_private=cfg.allow_private_sources)
            return clean
    except (crawler.ConfigError, apiconnector.ApiConfigError, UnsafeURL) as error:
        raise HTTPException(422, str(error)) from None
    raise HTTPException(422, "Source kind must be 'website' or 'api'. (Documents are added from the Library; database connectors are not implemented.)")


@router.get("")
def list_sources(admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    return [source_dict(db, s) for s in db.scalars(select(Source).order_by(Source.name))]


@router.post("", status_code=201)
def create_source(body: SourceBody, request: Request, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    if body.visibility not in VISIBILITIES:
        raise HTTPException(422, "Invalid visibility.")
    if db.scalar(select(Source).where(Source.name == body.name.strip())):
        raise HTTPException(409, "A source with this name already exists.")
    source = Source(
        name=body.name.strip(), kind=body.kind, config=_normalise(body.kind, body.config), visibility=body.visibility, enabled=body.enabled,
        schedule_minutes=max(15, body.schedule_minutes) if body.schedule_minutes else None,
        secret_enc=secret_box().encrypt(body.secret) if body.secret else None, created_by=admin.id,
    )
    db.add(source)
    db.commit()
    audit.record(db, "source_created", request=request, user=admin, target=source.name, detail={"kind": source.kind})
    return source_dict(db, source)


@router.patch("/{source_id}")
def update_source(source_id: str, body: SourcePatch, request: Request, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "Source not found.")
    if body.name:
        source.name = body.name.strip()
    if body.config is not None:
        source.config = _normalise(source.kind, body.config)
    if body.visibility:
        if body.visibility not in VISIBILITIES:
            raise HTTPException(422, "Invalid visibility.")
        source.visibility = body.visibility
        db.execute(update(Document).where(Document.source_id == source.id).values(visibility=body.visibility))
    if body.enabled is not None:
        source.enabled = body.enabled  # disabled sources are excluded from retrieval immediately
    if body.clear_schedule:
        source.schedule_minutes = None
    elif body.schedule_minutes:
        source.schedule_minutes = max(15, body.schedule_minutes)
    if body.clear_secret:
        source.secret_enc = None
    elif body.secret:
        source.secret_enc = secret_box().encrypt(body.secret)
    db.commit()
    audit.record(db, "source_updated", request=request, user=admin, target=source.name, detail={k: v for k, v in body.model_dump(exclude_none=True).items() if k not in {"secret", "config"}})
    return source_dict(db, source)


@router.delete("/{source_id}")
def delete_source(source_id: str, request: Request, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "Source not found.")
    from ..models import Chunk
    ids = [r[0] for r in db.execute(select(Chunk.id).join(Document, Document.id == Chunk.document_id).where(Document.source_id == source.id, Document.legacy.is_(False))).all()]
    qdrant().delete_points(ids)  # only chunks of this source's (non-legacy) documents
    name = source.name
    db.delete(source)  # documents/chunks cascade in PostgreSQL
    db.commit()
    audit.record(db, "source_deleted", request=request, user=admin, target=name, detail={"points_removed": len(ids)})
    return {"ok": True, "points_removed": len(ids)}


@router.post("/{source_id}/sync")
def sync_now(source_id: str, request: Request, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "Source not found.")
    pending = db.scalar(select(Job).where(Job.kind == "sync_source", Job.ref_id == source.id, Job.status.in_(("queued", "running"))))
    if pending:
        raise HTTPException(409, "A synchronisation is already queued or running for this source.")
    job = Job(kind="sync_source", ref_id=source.id, label=f"Sync {source.name}", created_by=admin.id)
    db.add(job)
    source.status = "syncing"
    db.commit()
    audit.record(db, "source_sync_requested", request=request, user=admin, target=source.name)
    return {"job_id": job.id}


@router.post("/{source_id}/test")
def test_source(source_id: str, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Real connectivity test: fetches the first URL (website: also checks robots.txt) and reports what was found."""
    source = db.get(Source, source_id)
    if not source:
        raise HTTPException(404, "Source not found.")
    cfg = get_config()
    fetcher = SafeFetcher(allow_private=cfg.allow_private_sources, timeout=20)
    try:
        if source.kind == "website":
            conf = crawler.validate_config(source.config)
            url = conf["start_urls"][0]
            allowed = crawler.Robots(fetcher, fetcher.user_agent).allowed(url)
            if not allowed:
                return {"ok": False, "message": "robots.txt disallows crawling the start URL. The platform will not fetch it."}
            result = fetcher.get(url)
            title = None
            links = 0
            if result.status < 400:
                from ..htmltext import html_to_text
                title, _text, found = html_to_text(result.content.decode("utf-8", "replace"), result.url)
                links = len(found)
            return {"ok": result.status < 400, "message": f"HTTP {result.status}", "title": title, "links_found": links, "robots_allowed": True}
        secret = secret_box().decrypt(source.secret_enc) if source.secret_enc else None
        conf = apiconnector.validate_config(source.config)
        records, _raw = apiconnector.fetch_records(conf, secret, fetcher)
        sample = apiconnector.flatten(records[0])[:6] if records else []
        return {"ok": True, "message": f"{len(records)} record(s) parsed", "sample": sample}
    except Exception as error:
        return {"ok": False, "message": f"{type(error).__name__}: {str(error)[:300]}"}
