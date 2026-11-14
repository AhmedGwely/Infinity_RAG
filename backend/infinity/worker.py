"""Background worker: durable PostgreSQL job queue, scheduled source sync and Qdrant bootstrap.

Jobs live in PostgreSQL, so queued work survives restarts; jobs left 'running' by a crash/reboot are re-queued.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text, update

from . import apiconnector, crawler
from .adopt import adopt_legacy
from .config import get_config
from .db import session_scope
from .fetcher import SafeFetcher
from .ingest import index_parsed, ingest_document
from .models import Document, Job, Source
from .services import embedder, qdrant, redis_client, secret_box

log = logging.getLogger("infinity.worker")
MAX_ATTEMPTS = 3
_stop = False


def _progress(job_id: int):
    def update_message(message: str) -> None:
        with session_scope() as s:
            s.execute(update(Job).where(Job.id == job_id).values(message=message[:300]))
    return update_message


def claim_job() -> int | None:
    with session_scope() as s:
        row = s.execute(text("""
            UPDATE jobs SET status='running', started_at=now(), attempts=attempts+1
             WHERE id = (SELECT id FROM jobs WHERE status='queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
         RETURNING id""")).first()
        return row[0] if row else None


def recover_stale() -> None:
    with session_scope() as s:
        s.execute(text("UPDATE jobs SET status='queued', message='re-queued after restart' WHERE status='running' AND attempts < :m"), {"m": MAX_ATTEMPTS})
        s.execute(text("UPDATE jobs SET status='failed', error='interrupted too many times', finished_at=now() WHERE status='running'"))
        s.execute(text("UPDATE sources SET status='idle' WHERE status='syncing' AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.ref_id = sources.id AND j.status IN ('queued','running'))"))
        s.execute(text("UPDATE documents SET status='queued' WHERE status='processing' AND file_path IS NOT NULL AND NOT legacy"))


def _run_ingest(session, job: Job) -> dict:
    count = ingest_document(session, job.ref_id, qdrant(), embedder())
    return {"chunks": count}


def _run_sync(session, job: Job, progress) -> dict:
    source = session.get(Source, job.ref_id)
    if source is None:
        raise LookupError("source no longer exists")
    cfg = get_config()
    source.status = "syncing"
    session.commit()
    fetcher = SafeFetcher(allow_private=cfg.allow_private_sources)
    indexer = lambda s, d, p: index_parsed(s, d, p, qdrant(), embedder())  # noqa: E731
    try:
        if source.kind == "website":
            stats = crawler.sync_website(session, source, fetcher=fetcher, indexer=indexer, upload_dir=cfg.upload_dir, progress=progress)
        elif source.kind == "api":
            secret = secret_box().decrypt(source.secret_enc) if source.secret_enc else None
            stats = apiconnector.sync_api(session, source, secret, fetcher=fetcher, indexer=indexer)
        else:
            raise ValueError(f"unsupported source kind {source.kind}")
        source = session.get(Source, source.id)
        errors = stats.get("error_count", 0)
        source.status = "ok" if not errors else "error"
        source.last_error = f"{errors} page(s) failed; see job details" if errors else None
        source.last_sync_at = datetime.now(timezone.utc)
        session.commit()
        return stats
    except Exception as error:
        session.rollback()
        source = session.get(Source, job.ref_id)
        if source:
            source.status, source.last_error = "error", f"{type(error).__name__}: {str(error)[:400]}"
            session.commit()
        raise


_TRANSIENT_NAMES = {"EmbedderUnavailable", "ConnectError", "ConnectTimeout", "ReadTimeout", "ConnectionError", "ResponseHandlingException", "OperationalError"}


def _transient(error: Exception) -> bool:
    """Dependencies that are merely still starting (typical right after a reboot) should be retried, not failed."""
    return any(cls.__name__ in _TRANSIENT_NAMES for cls in type(error).__mro__) or type(error).__name__ in _TRANSIENT_NAMES


def execute(job_id: int) -> None:
    progress = _progress(job_id)
    with session_scope() as s:
        job = s.get(Job, job_id)
        kind = job.kind
    try:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if kind == "ingest_file":
                stats = _run_ingest(s, job)
            elif kind == "sync_source":
                stats = _run_sync(s, job, progress)
            elif kind == "adopt_legacy":
                stats = adopt_legacy(s, qdrant(), progress)
            else:
                raise ValueError(f"unknown job kind {kind}")
        with session_scope() as s:
            s.execute(update(Job).where(Job.id == job_id).values(status="done", stats=stats, finished_at=datetime.now(timezone.utc), message="completed", error=None))
        log.info("job %s (%s) done: %s", job_id, kind, stats)
    except Exception as error:
        log.exception("job %s (%s) failed", job_id, kind)
        retry = kind == "adopt_legacy" or _transient(error)
        with session_scope() as s:
            job = s.get(Job, job_id)
            requeue = retry and job.attempts < MAX_ATTEMPTS
            job.status = "queued" if requeue else "failed"
            job.error = f"{type(error).__name__}: {str(error)[:800]}"
            job.finished_at = None if requeue else datetime.now(timezone.utc)


def schedule_due_sources() -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        for source in s.scalars(select(Source).where(Source.enabled.is_(True), Source.schedule_minutes.is_not(None), Source.status != "syncing")):
            due = source.last_sync_at is None or now - source.last_sync_at >= timedelta(minutes=source.schedule_minutes)
            pending = s.scalar(select(func.count()).select_from(Job).where(Job.kind == "sync_source", Job.ref_id == source.id, Job.status.in_(("queued", "running"))))
            if due and not pending:
                s.add(Job(kind="sync_source", ref_id=source.id, label=f"Scheduled sync: {source.name}"))
                source.status = "syncing"
                log.info("scheduled sync for %s", source.name)


def ensure_qdrant_and_mirror() -> bool:
    """Retry-safe: Qdrant may start after this container (e.g. after a reboot). Returns True once settled."""
    try:
        state = qdrant().ensure_collection()
        points = qdrant().count()
    except Exception as error:
        log.warning("Qdrant not ready yet: %s", type(error).__name__)
        return False
    with session_scope() as s:
        mirrored = s.scalar(select(func.count()).select_from(Document).where(Document.legacy.is_(True))) or 0
        active = s.scalar(select(func.count()).select_from(Job).where(Job.kind == "adopt_legacy", Job.status.in_(("queued", "running")))) or 0
        if points > 0 and mirrored == 0 and not active:
            s.add(Job(kind="adopt_legacy", label="Mirror existing Qdrant collection (read-only, automatic)"))
            log.info("collection %s is %s with %d points; queued read-only mirror", get_config().collection, state, points)
    return True


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("_stop", True))
    for attempt in range(60):  # wait for the API container to finish migrations
        try:
            recover_stale()
            break
        except Exception as error:
            log.warning("database not ready (%s); retrying", type(error).__name__)
            time.sleep(3)
    settled, last_maint, last_sched = False, 0.0, 0.0
    log.info("worker started")
    while not _stop:
        now = time.monotonic()
        try:
            redis_client().set("infinity:worker:heartbeat", datetime.now(timezone.utc).isoformat(), ex=60)
        except Exception:
            pass
        if not settled and now - last_maint > 20:
            settled, last_maint = ensure_qdrant_and_mirror(), now
        if now - last_sched > 60:
            last_sched = now
            try:
                schedule_due_sources()
            except Exception:
                log.exception("scheduler error")
        try:
            job_id = claim_job()
        except Exception:
            log.exception("could not claim job")
            time.sleep(5)
            continue
        if job_id is None:
            time.sleep(2)
            continue
        execute(job_id)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
