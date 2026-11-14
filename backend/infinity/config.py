"""Environment-driven configuration for the platform services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy.engine import URL


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def build_database_url() -> str:
    """Single source of truth for the PostgreSQL connection.

    DATABASE_URL, if set, wins (tests / external databases). Otherwise the URL is assembled from the discrete
    POSTGRES_* variables with SQLAlchemy's URL builder, which percent-encodes the password, so characters such as
    @ : / # ? % + $ or spaces can never corrupt the URL (a hand-built f-string URL breaks on them).
    """
    explicit = os.getenv("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    return URL.create(
        "postgresql+psycopg",
        username=os.getenv("POSTGRES_USER", "infinity"),
        password=os.getenv("POSTGRES_PASSWORD") or None,
        host=os.getenv("POSTGRES_HOST", "postgres"),
        port=_int("POSTGRES_PORT", 5432),
        database=os.getenv("POSTGRES_DB", "infinity"),
    ).render_as_string(hide_password=False)


@dataclass(frozen=True)
class Config:
    database_url: str
    redis_url: str
    qdrant_url: str
    qdrant_api_key: str | None
    collection: str
    qdrant_allow_create: bool
    embedder_url: str
    secret_key: str
    upload_dir: str
    cookie_secure: bool
    session_hours: int
    bootstrap_email: str
    bootstrap_password: str
    allow_private_sources: bool
    max_upload_mb: int
    retrieval_vector_k: int
    retrieval_lexical_k: int
    context_chunks: int
    # Legacy .env values are used only as a fallback until an admin saves provider settings in the GUI.
    llm_provider: str
    llm_model: str
    gemini_api_key: str | None
    gemini_model: str
    openai_api_key: str | None
    ollama_base_url: str
    legacy_visibility: str


@lru_cache(maxsize=1)
def get_config() -> Config:
    secret = os.getenv("APP_SECRET_KEY", "")
    return Config(
        database_url=build_database_url(),
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        qdrant_url=os.getenv("PLATFORM_QDRANT_URL", "http://host.docker.internal:6333"),
        qdrant_api_key=os.getenv("QDRANT_API_KEY") or None,
        collection=os.getenv("COLLECTION_NAME", "rd_engineering"),
        qdrant_allow_create=_bool("QDRANT_ALLOW_CREATE", True),
        embedder_url=os.getenv("EMBEDDER_URL", "http://embedder:8100"),
        secret_key=secret,
        upload_dir=os.getenv("UPLOAD_DIR", "/data/uploads"),
        cookie_secure=_bool("COOKIE_SECURE", False),
        session_hours=_int("SESSION_HOURS", 12),
        bootstrap_email=os.getenv("BOOTSTRAP_ADMIN_EMAIL", "admin@infinity.local"),
        bootstrap_password=os.getenv("BOOTSTRAP_ADMIN_PASSWORD", ""),
        allow_private_sources=_bool("ALLOW_PRIVATE_NETWORK_SOURCES", False),
        max_upload_mb=_int("MAX_UPLOAD_MB", 100),
        retrieval_vector_k=_int("RETRIEVAL_VECTOR_K", 24),
        retrieval_lexical_k=_int("RETRIEVAL_LEXICAL_K", 24),
        context_chunks=_int("CONTEXT_CHUNKS", 8),
        llm_provider=os.getenv("LLM_PROVIDER", "gemini").strip().lower(),
        llm_model=os.getenv("LLM_MODEL", "qwen3:8b"),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        ollama_base_url=os.getenv("PLATFORM_OLLAMA_URL", "http://host.docker.internal:11434"),
        legacy_visibility=os.getenv("LEGACY_DOCUMENT_VISIBILITY", "internal"),
    )
