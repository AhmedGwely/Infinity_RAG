"""Hybrid dense and keyword retrieval interface."""

from app.core.schemas import SearchHit


class HybridSearch:
    """Combine semantic and lexical results without dropping provenance."""

    def search(self, question: str, limit: int = 5) -> list[SearchHit]:
        """Run keyword and vector search, then fuse the ranked result lists."""
        raise NotImplementedError("TODO: implement hybrid retrieval and rank fusion.")
