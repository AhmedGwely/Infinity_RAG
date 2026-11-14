"""SQLAlchemy engine/session management."""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_config


class Base(DeclarativeBase):
    pass


@lru_cache(maxsize=1)
def get_engine():
    return create_engine(get_config().database_url, pool_pre_ping=True, pool_size=8, max_overflow=8)


@lru_cache(maxsize=1)
def get_sessionmaker():
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope():
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
