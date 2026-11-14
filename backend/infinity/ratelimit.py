"""Fixed-window rate limiting in Redis. Fails open (and logs) if Redis is unreachable."""

from __future__ import annotations

import logging

from fastapi import HTTPException

from .services import redis_client

log = logging.getLogger(__name__)


def limit(key: str, max_calls: int, window_seconds: int) -> None:
    try:
        r = redis_client()
        name = f"rl:{key}"
        count = r.incr(name)
        if count == 1:
            r.expire(name, window_seconds)
        if count > max_calls:
            raise HTTPException(status_code=429, detail="Too many requests. Please wait a moment and try again.")
    except HTTPException:
        raise
    except Exception as error:
        log.warning("rate limiter unavailable (%s); allowing request", type(error).__name__)
