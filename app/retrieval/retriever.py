"""Semantic retrieval orchestration."""

from app.core.schemas import SearchHit
from app.embeddings.embedding_service import EmbeddingService
from app.vectorstore.qdrant_store import VectorStore


class Retriever:
    """Embed a query and delegate nearest-neighbor search to a vector store."""

    def __init__(self, embeddings: EmbeddingService, store: VectorStore) -> None:
        self._embeddings = embeddings
        self._store = store

    def retrieve(self, question: str, limit: int = 5) -> list[SearchHit]:
        """Embed a question and return the nearest matching chunks."""
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        vector = self._embeddings.embed_query(question)
        return self._store.search(vector, limit)
