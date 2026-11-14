from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import audit, providers
from ..config import get_config
from ..deps import admin_only, current_user, engineer_up, get_db
from ..ratelimit import limit
from ..models import AuditLog, Chunk, Document, Job, Source, User
from ..services import embedder, qdrant, redis_client

router = APIRouter(tags=["admin"])


def _job(j: Job) -> dict:
    return {"id": j.id, "kind": j.kind, "label": j.label, "status": j.status, "attempts": j.attempts, "message": j.message, "error": j.error,
            "stats": j.stats, "created_at": j.created_at, "started_at": j.started_at, "finished_at": j.finished_at}


@router.get("/jobs")
def jobs(status: str | None = None, user: User = Depends(engineer_up), db: Session = Depends(get_db)):
    stmt = select(Job)
    if user.role != "admin":
        stmt = stmt.where(Job.created_by == user.id)
    if status:
        stmt = stmt.where(Job.status == status)
    return [_job(j) for j in db.scalars(stmt.order_by(Job.id.desc()).limit(150))]


@router.post("/jobs/{job_id}/retry")
def retry(job_id: int, request: Request, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job or job.status != "failed":
        raise HTTPException(400, "Only failed jobs can be retried.")
    job.status, job.error, job.attempts, job.finished_at = "queued", None, 0, None
    db.commit()
    audit.record(db, "job_retried", request=request, user=admin, target=str(job_id))
    return _job(job)


@router.get("/overview")
def overview(user: User = Depends(current_user), db: Session = Depends(get_db)):
    docs = dict(db.execute(select(Document.status, func.count()).group_by(Document.status)).all())
    return {
        "documents": docs, "chunks": db.scalar(select(func.count()).select_from(Chunk)) or 0,
        "sources": db.scalar(select(func.count()).select_from(Source)) or 0,
        "legacy_documents": db.scalar(select(func.count()).select_from(Document).where(Document.legacy.is_(True))) or 0,
        "jobs_active": db.scalar(select(func.count()).select_from(Job).where(Job.status.in_(("queued", "running")))) or 0,
        "jobs_failed": db.scalar(select(func.count()).select_from(Job).where(Job.status == "failed")) or 0,
    }


@router.get("/admin/health")
def health_detail(admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    cfg = get_config()
    out: dict = {}
    started = time.monotonic()
    try:
        db.execute(text("SELECT 1"))
        out["postgres"] = {"ok": True, "ms": int((time.monotonic() - started) * 1000)}
    except Exception as error:
        out["postgres"] = {"ok": False, "error": type(error).__name__}
    try:
        r = redis_client()
        r.ping()
        beat = r.get("infinity:worker:heartbeat")
        out["redis"] = {"ok": True}
        out["worker"] = {"ok": bool(beat), "detail": "heartbeat seen" if beat else "no heartbeat in the last 60 s"}
    except Exception as error:
        out["redis"] = {"ok": False, "error": type(error).__name__}
        out["worker"] = {"ok": False, "detail": "unknown (Redis unreachable)"}
    try:
        q = qdrant()
        points = q.count()
        mirrored = db.scalar(select(func.count()).select_from(Chunk)) or 0
        out["qdrant"] = {"ok": True, "url": cfg.qdrant_url, "collection": cfg.collection, "points": points, "mirrored_chunks": mirrored}
    except Exception as error:
        out["qdrant"] = {"ok": False, "url": cfg.qdrant_url, "collection": cfg.collection, "error": f"{type(error).__name__}: {str(error)[:160]}"}
    emb = embedder().health()
    out["embedder"] = {"ok": emb.get("status") == "ok", **emb}
    view = providers.public_view(db)
    out["llm"] = {"configured": bool(view["keys"].get(view["provider"], {}).get("configured")) or view["provider"] == "ollama", "provider": view["provider"], "model": view["models"].get(view["provider"])}
    return out


@router.get("/admin/audit")
def audit_log(action: str | None = None, limit_n: int = 200, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    rows = db.scalars(stmt.order_by(AuditLog.id.desc()).limit(min(limit_n, 1000)))
    return [{"id": a.id, "ts": a.ts, "user": a.user_email, "action": a.action, "target": a.target, "detail": a.detail, "ip": a.ip} for a in rows]


class LlmSettings(BaseModel):
    provider: str | None = None
    models: dict | None = None
    ollama_base_url: str | None = None
    keys: dict | None = None  # {"gemini": "...", "openai": "..."}; empty string clears


@router.get("/admin/settings/llm")
def get_llm(admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    return providers.public_view(db)


@router.put("/admin/settings/llm")
def put_llm(body: LlmSettings, request: Request, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    try:
        providers.save(db, provider=body.provider, models=body.models, ollama_base_url=body.ollama_base_url, keys=body.keys)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    db.commit()
    audit.record(db, "llm_settings_changed", request=request, user=admin,
                 detail={"provider": body.provider, "models": body.models, "keys_changed": sorted((body.keys or {}).keys())})
    return providers.public_view(db)


class ReplaceKey(BaseModel):
    provider: str
    api_key: str = Field(min_length=1, max_length=512)
    verify: bool = True  # validate with the provider before storing; False = store without validating (explicit admin choice)


@router.post("/admin/settings/llm/credentials")
def replace_llm_credential(body: ReplaceKey, request: Request, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Replace ONE provider key. Works when the stored credential is unreadable (it is never decrypted).
    Order: validate with the provider -> single DB transaction (lock, encrypt with the current key, audit) -> commit.
    Any failure before the commit leaves the database exactly as it was."""
    limit(f"llm-key:{admin.id}", 10, 60)
    if body.provider not in ("gemini", "openai"):
        raise HTTPException(422, "provider must be gemini or openai")
    warning = None
    if body.verify:
        model = providers.effective(db)["models"].get(body.provider)
        try:
            warning = providers.validate_key(body.provider, body.api_key.strip(), model)["model_warning"]
        except providers.CredentialRejected as error:
            return JSONResponse({"detail": f"{error} Nothing was saved.", "code": "key_rejected"}, status_code=422)
        except providers.ValidationUnavailable as error:
            return JSONResponse({"detail": f"{error} Nothing was saved. Try again, or choose “Save without validating”.", "code": "validation_unavailable"}, status_code=502)
    try:
        outcome = providers.replace_credential(db, body.provider, body.api_key)
        audit.record(db, "llm_credential_replaced", request=request, user=admin, target=body.provider, commit=False,
                     detail={"provider": body.provider, "validated": body.verify, "archived_previous": outcome["archived_previous"]})
        db.commit()  # the key row and its audit entry become visible together, or not at all
    except ValueError as error:
        db.rollback()
        raise HTTPException(422, str(error)) from None
    except Exception:
        db.rollback()
        raise
    label = {"gemini": "Gemini", "openai": "OpenAI"}[body.provider]
    view = providers.public_view(db)
    view["result"] = {"ok": True, "provider": body.provider, "validated": body.verify, "archived_previous": outcome["archived_previous"], "warning": warning,
                      "message": f"{label} API key saved" + (" and verified with the provider." if body.verify else " (not validated).")
                                 + (" The previous unreadable credential was kept encrypted and set aside." if outcome["archived_previous"] else "")}
    return view


class TestBody(BaseModel):
    provider: str | None = None


@router.post("/admin/settings/llm/test")
def test_llm(body: TestBody, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    return providers.test_connection(db, body.provider)


@router.post("/admin/adopt-legacy")
def adopt(request: Request, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Queue a read-only mirror of the pre-existing Qdrant collection into PostgreSQL."""
    if db.scalar(select(Job).where(Job.kind == "adopt_legacy", Job.status.in_(("queued", "running")))):
        raise HTTPException(409, "A mirror job is already queued or running.")
    job = Job(kind="adopt_legacy", label="Mirror existing Qdrant collection (read-only)", created_by=admin.id)
    db.add(job)
    db.commit()
    audit.record(db, "adopt_legacy_requested", request=request, user=admin)
    return _job(job)
