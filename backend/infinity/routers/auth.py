from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_config
from ..crypto import hash_password, new_token, password_problems, token_hash, verify_password
from ..deps import COOKIE, current_user, get_db
from ..models import User, UserSession
from ..ratelimit import limit

router = APIRouter(tags=["auth"])
_DUMMY_HASH = hash_password("dummy-password-for-timing")


class LoginBody(BaseModel):
    email: str
    password: str


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


def user_dict(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role, "is_active": user.is_active,
            "created_at": user.created_at, "last_login_at": user.last_login_at}


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    ip = audit.client_ip(request) or "unknown"
    limit(f"login:{ip}", 20, 60)
    limit(f"login:{email}", 8, 60)
    user = db.scalar(select(User).where(User.email == email))
    ok = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)  # constant-ish time either way
    if not user or not ok or not user.is_active:
        audit.record(db, "login_failed", request=request, email=email)
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    cfg = get_config()
    token = new_token()
    expires = datetime.now(timezone.utc) + timedelta(hours=cfg.session_hours)
    db.add(UserSession(id=token_hash(token), user_id=user.id, expires_at=expires, ip=ip[:64], user_agent=(request.headers.get("user-agent") or "")[:300]))
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    response.set_cookie(COOKIE, token, max_age=cfg.session_hours * 3600, httponly=True, samesite="lax", secure=cfg.cookie_secure, path="/")
    audit.record(db, "login", request=request, user=user)
    return user_dict(user)


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(COOKIE)
    if token:
        db.execute(delete(UserSession).where(UserSession.id == token_hash(token)))
        db.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return user_dict(user)


@router.post("/auth/password")
def change_password(body: PasswordBody, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    problems = password_problems(body.new_password)
    if problems:
        raise HTTPException(status_code=422, detail="New password needs: " + ", ".join(problems) + ".")
    user.password_hash = hash_password(body.new_password)
    keep = token_hash(request.cookies.get(COOKIE, ""))
    db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.id != keep))  # sign out other devices
    db.commit()
    audit.record(db, "password_changed", request=request, user=user)
    return {"ok": True}
