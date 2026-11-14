"""ORM models. The authoritative DDL lives in migrations/versions/0001_initial.py."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


TZ = DateTime(timezone=True)

ROLES = ("admin", "engineer", "viewer")
VISIBILITIES = ("public", "internal", "confidential")
CLEARANCE = {
    "viewer": {"public"},
    "engineer": {"public", "internal"},
    "admin": {"public", "internal", "confidential"},
}


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="engineer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TZ, default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class UserSession(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the cookie token
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(TZ, default=_now)
    expires_at: Mapped[datetime] = mapped_column(TZ)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(20))  # website | api
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(String(20), default="internal")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    schedule_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="idle")  # idle | syncing | ok | error
    last_sync_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ, default=_now)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    doc_type: Mapped[str] = mapped_column(String(20), default="")
    uri: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visibility: Mapped[str] = mapped_column(String(20), default="internal")
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|processing|ready|failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    issues: Mapped[list] = mapped_column(JSONB, default=list)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    legacy: Mapped[bool] = mapped_column(Boolean, default=False)  # mirrored from the pre-existing Qdrant index
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    uploaded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TZ, default=_now, onupdate=_now)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    source: Mapped["Source"] = relationship(lazy="joined")


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # equals the Qdrant point id
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    ord: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    section_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    locator: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZ, default=_now)
    # tsv (generated tsvector) exists only in the database and is queried with raw SQL.


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(30))  # ingest_file | sync_source | adopt_legacy
    ref_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    label: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="New conversation")
    created_at: Mapped[datetime] = mapped_column(TZ, default=_now)
    updated_at: Mapped[datetime] = mapped_column(TZ, default=_now, onupdate=_now)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZ, default=_now)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="report")  # report | comparison
    title: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(TZ, default=_now)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TZ, default=_now, onupdate=_now)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZ, default=_now, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    target: Mapped[str | None] = mapped_column(String(300), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
