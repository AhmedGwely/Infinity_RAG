"""Provider-independent embedding interface and local BGE-M3 adapter."""

from __future__ import annotations

import math
from typing import Protocol

from app.core.config import Settings, load_settings

EMBEDDING_DIMENSION = 1024


class EmbeddingService(Protocol):
    """Interface for local or hosted embedding implementations."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per document chunk."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Return a vector for a search query."""
        ...


class BGEEmbeddingService:
    """Generate normalized local embeddings with the BAAI BGE-M3 model."""

    def __init__(
        self,
        model_name: str | None = None,
        *,
        device: str | None = None,
        batch_size: int = 32,
        model: object | None = None,
        settings: Settings | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        self.model_name = model_name or (settings or load_settings()).embedding_model
        self.device = device
        self.batch_size = batch_size
        self._model = model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed document text locally, returning normalized dense vectors."""
        if not isinstance(texts, list):
            raise TypeError("texts must be a list of strings")
        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("texts must contain non-empty strings")

        encoded = self._get_model().encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        rows = encoded.tolist() if hasattr(encoded, "tolist") else encoded
        if len(rows) != len(texts):
            raise ValueError("embedding model returned an unexpected number of vectors")

        vectors: list[list[float]] = []
        for row in rows:
            if len(row) != EMBEDDING_DIMENSION:
                raise ValueError(
                    f"embedding model returned a vector with {len(row)} dimensions; "
                    f"expected {EMBEDDING_DIMENSION}"
                )
            values = [float(value) for value in row]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("embedding model returned a non-finite vector value")
            magnitude = math.sqrt(sum(value * value for value in values))
            if magnitude == 0:
                raise ValueError("embedding model returned a zero vector")
            vectors.append([value / magnitude for value in values])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """Embed one query with the same model and normalization as documents."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("query text must be a non-empty string")
        return self.embed_documents([text])[0]

    def _get_model(self) -> object:
        """Load sentence-transformers and its model on first use only."""
        if self._model is None:
            device = self.device or self._default_device()
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as error:
                raise RuntimeError(
                    "Install sentence-transformers to use BGE-M3 embeddings."
                ) from error
            self._model = SentenceTransformer(self.model_name, device=device)
        return self._model

    @staticmethod
    def _default_device() -> str:
        """Prefer CUDA when PyTorch reports it is available."""
        try:
            import torch
        except ImportError as error:
            raise RuntimeError("Install sentence-transformers and PyTorch to embed text.") from error
        return "cuda" if torch.cuda.is_available() else "cpu"
