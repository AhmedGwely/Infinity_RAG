"""Tests for semantic retrieval orchestration."""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from app.core.schemas import ChunkMetadata, DocumentChunk, SearchHit
from app.retrieval.retriever import Retriever


def test_retriever_embeds_question_and_returns_search_hits() -> None:
    metadata = ChunkMetadata(
        document_id="doc",
        document_name="Manual",
        document_type="pdf",
        page_number=2,
        section_title=None,
        chunk_id="chunk",
        source_file="manual.pdf",
        ingestion_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    expected_hits = [SearchHit(DocumentChunk("relevant text", metadata), 0.9)]
    embeddings = Mock()
    embeddings.embed_query.return_value = [0.25, 0.75]
    store = Mock()
    store.search.return_value = expected_hits
    retriever = Retriever(embeddings, store)

    hits = retriever.retrieve("Where is the register?", limit=3)

    embeddings.embed_query.assert_called_once_with("Where is the register?")
    store.search.assert_called_once_with([0.25, 0.75], 3)
    assert hits is expected_hits


@pytest.mark.parametrize(
    ("question", "limit"),
    [("", 5), ("question", 0), ("question", -1), ("question", True)],
)
def test_retriever_rejects_invalid_question_or_limit(question, limit) -> None:
    embeddings = Mock()
    store = Mock()

    with pytest.raises(ValueError):
        Retriever(embeddings, store).retrieve(question, limit)

    embeddings.embed_query.assert_not_called()
    store.search.assert_not_called()
