"""INFINITY AI API application."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from . import __version__
from .config import get_config
from .db import session_scope
from .models import User

log = logging.getLogger("infinity")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def bootstrap_admin() -> None:
    from sqlalchemy import func, select

    from .crypto import hash_password, password_problems

    cfg = get_config()
    with session_scope() as db:
        if db.scalar(select(func.count()).select_from(User)):
            return
        password = cfg.bootstrap_password
        if not password or password_problems(password):
            log.error("No users exist and BOOTSTRAP_ADMIN_PASSWORD is missing or weak; no admin was created. Run start.ps1 to generate one.")
            return
        db.add(User(email=cfg.bootstrap_email.lower(), name="Administrator", password_hash=hash_password(password), role="admin"))
        log.warning("Created bootstrap administrator %s", cfg.bootstrap_email)


def create_app() -> FastAPI:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = get_config()
    if len(cfg.secret_key) < 16:
        raise RuntimeError("APP_SECRET_KEY must be set (16+ characters). Run start.ps1 to generate it.")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        bootstrap_admin()
        yield

    app = FastAPI(title="INFINITY AI", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        # Browsers cannot add custom headers cross-site without CORS approval (none is configured), so this blocks CSRF.
        if request.method in UNSAFE and request.url.path.startswith("/api/") and request.headers.get("x-requested-with") != "infinity":
            return JSONResponse({"detail": "Missing request header."}, status_code=403)
        return await call_next(request)

    from app.core.exceptions import GenerationError, RAGError

    @app.exception_handler(GenerationError)
    async def generation_error(_request: Request, error: GenerationError):
        log.error("generation failed: %s", error)
        return JSONResponse({"detail": "Answer generation failed or returned invalid citations. Check the AI provider settings."}, status_code=502)

    from .providers import ProviderCredentialsError

    @app.exception_handler(ProviderCredentialsError)
    async def provider_credentials_error(_request: Request, error: ProviderCredentialsError):
        log.error("provider credentials unavailable: %s", error)
        return JSONResponse({"detail": str(error)[:400], "code": "provider_credentials"}, status_code=409)

    @app.exception_handler(RAGError)
    async def rag_error(_request: Request, error: RAGError):
        log.error("rag error: %s", error)
        return JSONResponse({"detail": str(error)[:300]}, status_code=400)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, error: Exception):
        # Log the full traceback server-side; give the browser a readable message instead of a bare "500".
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": f"Internal error ({type(error).__name__}). Check the api container logs for details."}, status_code=500)

    from .routers import admin, auth, chat, documents, reports, sources, users

    for module in (auth, users, documents, sources, chat, reports, admin):
        app.include_router(module.router, prefix="/api")

    @app.get("/api/health")
    def health():
        # Honest health: includes a real database round-trip, so a broken DB login can never look "healthy".
        try:
            with session_scope() as db:
                db.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            log.error("health check: database unavailable (%s)", type(exc).__name__)
            return JSONResponse({"status": "error", "database": "unavailable", "version": __version__}, status_code=503)
        return {"status": "ok", "database": "ok", "version": __version__}

    return app


app = create_app()
