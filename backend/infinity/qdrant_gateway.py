"""Guarded access to the Qdrant collection.

Safety rules for the pre-existing ``rd_engineering`` collection:
  * this module never calls delete_collection / recreate_collection;
  * a missing collection is created (identical 1024-d cosine config) only when allowed and only if absent;
  * point deletion is limited to explicit ids of non-legacy chunks owned by this platform.
"""

from __future__ import annotations

import logging
from typing import Iterator

from qdrant_client import QdrantClient, models

from .config import get_config

log = logging.getLogger(__name__)
EMBEDDING_DIMENSION = 1024


class QdrantGateway:
    def __init__(self) -> None:
        cfg = get_config()
        self.collection = cfg.collection
        self._allow_create = cfg.qdrant_allow_create
        self.client = QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key, timeout=30, prefer_grpc=False, check_compatibility=False)

    def ensure_collection(self) -> str:
        """Return 'existing' or 'created'. Never modifies an existing collection."""
        names = {c.name for c in self.client.get_collections().collections}
        if self.collection in names:
            info = self.client.get_collection(self.collection)
            vectors = info.config.params.vectors
            if not isinstance(vectors, models.VectorParams) or vectors.size != EMBEDDING_DIMENSION:
                raise RuntimeError(
                    f"collection {self.collection!r} exists but is not a single unnamed {EMBEDDING_DIMENSION}-d vector; refusing to use it"
                )
            return "existing"
        if not self._allow_create:
            raise RuntimeError(f"collection {self.collection!r} does not exist and QDRANT_ALLOW_CREATE is false")
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=models.VectorParams(size=EMBEDDING_DIMENSION, distance=models.Distance.COSINE),
        )
        log.warning("Created missing Qdrant collection %s", self.collection)
        return "created"

    def count(self) -> int:
        return self.client.count(collection_name=self.collection, exact=True).count

    def search(self, vector: list[float], limit: int) -> list[tuple[str, float]]:
        response = self.client.query_points(collection_name=self.collection, query=vector, limit=limit, with_payload=False)
        return [(str(p.id), float(p.score)) for p in response.points]

    def upsert(self, points: list[tuple[str, list[float], dict]]) -> None:
        if not points:
            return
        self.client.upsert(
            collection_name=self.collection,
            points=[models.PointStruct(id=pid, vector=vec, payload=payload) for pid, vec, payload in points],
            wait=True,
        )

    def delete_points(self, ids: list[str]) -> None:
        """Delete specific points by id (callers must exclude legacy chunks)."""
        if not ids:
            return
        self.client.delete(collection_name=self.collection, points_selector=models.PointIdsList(points=ids), wait=True)

    def scroll_payloads(self, batch: int = 128) -> Iterator[list]:
        """Read-only iteration over every point's payload (no vectors)."""
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.collection, limit=batch, offset=offset, with_payload=True, with_vectors=False
            )
            if points:
                yield points
            if offset is None:
                break

    def existing_ids(self, ids: list[str]) -> set[str]:
        found = self.client.retrieve(collection_name=self.collection, ids=ids, with_payload=False, with_vectors=False)
        return {str(p.id) for p in found}
