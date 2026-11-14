"""FastAPI dependencies: database session, session-cookie authentication and role checks."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .crypto import token_hash
from .db import get_sessionmaker
from .models import CLEARANCE, Document, User, UserSession

COOKIE = "infinity_session"


def get_db():
    db = get_sessionmaker()()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    session = db.get(UserSession, token_hash(token))
    if session is None or session.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Account disabled.")
    return user


def require_role(*roles: str):
    def checker(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="You do not have permission to do this.")
        return user
    return checker


admin_only = require_role("admin")
engineer_up = require_role("admin", "engineer")


def can_view(user: User, doc: Document) -> bool:
    return doc.visibility in CLEARANCE.get(user.role, {"public"})
