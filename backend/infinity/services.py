"""Lazily-created shared service clients. Constructing them never contacts the network."""

from __future__ import annotations

from functools import lru_cache

from .config import get_config
from .crypto import SecretBox
from .embedder_client import EmbedderClient
from .qdrant_gateway import QdrantGateway


@lru_cache(maxsize=1)
def qdrant() -> QdrantGateway:
    return QdrantGateway()


@lru_cache(maxsize=1)
def embedder() -> EmbedderClient:
    return EmbedderClient()


@lru_cache(maxsize=1)
def secret_box() -> SecretBox:
    return SecretBox(get_config().secret_key)


@lru_cache(maxsize=1)
def redis_client():
    import redis
    return redis.Redis.from_url(get_config().redis_url, socket_timeout=2, socket_connect_timeout=2, decode_responses=True)
