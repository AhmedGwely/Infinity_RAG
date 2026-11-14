from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import audit
from ..crypto import hash_password, password_problems
from ..deps import admin_only, get_db
from ..models import ROLES, User, UserSession
from .auth import user_dict

router = APIRouter(prefix="/users", tags=["users"])
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class NewUser(BaseModel):
    email: str
    name: str = ""
    password: str
    role: str = "engineer"


class UserPatch(BaseModel):
    name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None


def _active_admins(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))) or 0


@router.get("")
def list_users(admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    return [user_dict(u) for u in db.scalars(select(User).order_by(User.created_at))]


@router.post("", status_code=201)
def create_user(body: NewUser, request: Request, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if not _EMAIL.match(email):
        raise HTTPException(422, "Enter a valid email address.")
    if body.role not in ROLES:
        raise HTTPException(422, f"Role must be one of: {', '.join(ROLES)}.")
    problems = password_problems(body.password)
    if problems:
        raise HTTPException(422, "Password needs: " + ", ".join(problems) + ".")
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "A user with this email already exists.")
    user = User(email=email, name=body.name.strip(), password_hash=hash_password(body.password), role=body.role)
    db.add(user)
    db.commit()
    audit.record(db, "user_created", request=request, user=admin, target=email, detail={"role": body.role})
    return user_dict(user)


@router.patch("/{user_id}")
def patch_user(user_id: str, body: UserPatch, request: Request, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found.")
    changes: dict = {}
    if body.role is not None:
        if body.role not in ROLES:
            raise HTTPException(422, f"Role must be one of: {', '.join(ROLES)}.")
        if user.role == "admin" and body.role != "admin" and _active_admins(db) <= 1:
            raise HTTPException(400, "You cannot demote the last administrator.")
        user.role, changes["role"] = body.role, body.role
    if body.is_active is not None:
        if user.role == "admin" and not body.is_active and _active_admins(db) <= 1:
            raise HTTPException(400, "You cannot disable the last administrator.")
        user.is_active, changes["is_active"] = body.is_active, body.is_active
        if not body.is_active:
            db.execute(delete(UserSession).where(UserSession.user_id == user.id))
    if body.name is not None:
        user.name, changes["name"] = body.name.strip(), body.name.strip()
    if body.password:
        problems = password_problems(body.password)
        if problems:
            raise HTTPException(422, "Password needs: " + ", ".join(problems) + ".")
        user.password_hash = hash_password(body.password)
        db.execute(delete(UserSession).where(UserSession.user_id == user.id))
        changes["password"] = "reset"
    db.commit()
    audit.record(db, "user_updated", request=request, user=admin, target=user.email, detail=changes)
    return user_dict(user)


@router.delete("/{user_id}")
def delete_user(user_id: str, request: Request, admin: User = Depends(admin_only), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found.")
    if user.id == admin.id:
        raise HTTPException(400, "You cannot delete your own account.")
    if user.role == "admin" and user.is_active and _active_admins(db) <= 1:
        raise HTTPException(400, "You cannot delete the last administrator.")
    email = user.email
    db.delete(user)
    db.commit()
    audit.record(db, "user_deleted", request=request, user=admin, target=email)
    return {"ok": True}
