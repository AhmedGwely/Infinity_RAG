"""HTTP client for the shared BGE-M3 embedding service (loaded once to save RAM)."""

from __future__ import annotations

import httpx

from .config import get_config

BATCH = 16


class EmbedderUnavailable(RuntimeError):
    pass


class EmbedderClient:
    def __init__(self) -> None:
        self.base = get_config().embedder_url.rstrip("/")

    def health(self) -> dict:
        try:
            r = httpx.get(f"{self.base}/health", timeout=5)
            return r.json()
        except Exception as error:
            return {"status": "unavailable", "detail": type(error).__name__}

    def embed(self, texts: list[str], kind: str = "document") -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            batch = texts[start:start + BATCH]
            try:
                r = httpx.post(f"{self.base}/embed", json={"texts": batch, "kind": kind}, timeout=600)
                r.raise_for_status()
            except httpx.HTTPError as error:
                raise EmbedderUnavailable(f"embedding service error: {type(error).__name__}") from error
            out.extend(r.json()["vectors"])
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text], kind="query")[0]
