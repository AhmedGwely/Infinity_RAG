"""Optional reranking interface."""

from typing import Protocol

from app.core.schemas import SearchHit


class Reranker(Protocol):
    """Interface for an optional cross-encoder or provider reranker."""

    def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        """Return hits reordered by query-document relevance."""
        ...
