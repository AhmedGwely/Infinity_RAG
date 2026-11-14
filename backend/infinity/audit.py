"""Append-only audit trail."""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from .models import AuditLog, User


def client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def record(db: Session, action: str, *, request: Request | None = None, user: User | None = None, email: str | None = None,
           target: str | None = None, detail: dict | None = None, commit: bool = True) -> None:
    db.add(AuditLog(
        action=action, user_id=user.id if user else None, user_email=(user.email if user else email),
        target=(target or None) and target[:300], detail=detail or {}, ip=client_ip(request),
    ))
    if commit:
        db.commit()  # commit=False lets the caller make the audit row part of its own single transaction
