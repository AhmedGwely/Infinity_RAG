"""In-memory tests for Qdrant payload storage and retrieval."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient

from app.core.config import Settings
from app.core.schemas import (
    ChunkMetadata,
    DocumentChunk,
    ExtractedTable,
    PageReference,
)
from app.embeddings.embedding_service import EMBEDDING_DIMENSION
from app.vectorstore.qdrant_store import QdrantStore


def make_chunk() -> DocumentChunk:
    return DocumentChunk(
        text="Voltage register details and its table.",
        metadata=ChunkMetadata(
            document_id="document-1",
            document_name="Engineering manual",
            document_type="pdf",
            page_number=4,
            section_title="Registers",
            chunk_id=str(uuid4()),
            source_file="manual.pdf",
            ingestion_date=datetime(2026, 2, 3, 10, 30, tzinfo=timezone.utc),
            document_version="v2",
            section_number="4.1",
            source_pages=(
                PageReference(pdf_page=4, printed_page=None),
                PageReference(pdf_page=5, printed_page=3),
            ),
        ),
        tables=(
            ExtractedTable(
                table_id="table-1",
                pdf_page=4,
                printed_page=None,
                caption="Voltage values",
                column_headers=("Address", "Description"),
                rows=(("0x1000", "Phase A"),),
                markdown="| Address | Description |",
                section_number="4.1",
                section_title="Registers",
                needs_review=True,
            ),
            ExtractedTable(
                table_id="table-2",
                pdf_page=5,
                printed_page=3,
                caption=None,
                column_headers=("Code",),
                rows=(("OK",),),
                markdown="| Code |",
            ),
        ),
    )


def make_store(client: QdrantClient, collection_name: str = "test_chunks") -> QdrantStore:
    return QdrantStore(
        Settings(collection_name=collection_name),
        client=client,
    )


def test_qdrant_round_trip_preserves_metadata_pages_and_tables() -> None:
    client = QdrantClient(":memory:")
    store = make_store(client)
    original = make_chunk()
    vector = [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)

    store.upsert([original], [vector])
    hits = store.search(vector, limit=1)

    assert len(hits) == 1
    assert hits[0].chunk == original
    assert hits[0].score == pytest.approx(1.0)
    assert hits[0].chunk.metadata.source_pages == (
        PageReference(pdf_page=4, printed_page=None),
        PageReference(pdf_page=5, printed_page=3),
    )
    assert hits[0].chunk.tables == original.tables
    client.close()


def test_collection_creation_is_idempotent_for_missing_collection() -> None:
    client = QdrantClient(":memory:")

    first_store = make_store(client, "new_collection")
    second_store = make_store(client, "new_collection")

    assert first_store.collection_name == second_store.collection_name
    collection = client.get_collection("new_collection")
    assert collection.config.params.vectors.size == EMBEDDING_DIMENSION
    assert collection.config.params.vectors.distance.name == "COSINE"
    assert [item.name for item in client.get_collections().collections] == ["new_collection"]
    client.close()


def test_store_can_refuse_to_create_a_missing_collection() -> None:
    client = QdrantClient(":memory:")

    with pytest.raises(RuntimeError, match="does not exist"):
        QdrantStore(
            Settings(collection_name="read_only_missing"),
            client=client,
            create_if_missing=False,
        )

    assert client.get_collections().collections == []
    client.close()


def test_upsert_accepts_empty_input_and_rejects_mismatched_counts() -> None:
    client = QdrantClient(":memory:")
    store = make_store(client)

    store.upsert([], [])
    with pytest.raises(ValueError, match="counts must match"):
        store.upsert([make_chunk()], [])
    with pytest.raises(ValueError, match="1024 dimensions"):
        store.upsert([make_chunk()], [[0.0]])

    client.close()


@pytest.mark.parametrize(
    "vector",
    [
        [1.0, 0.0],
        [float("nan")] + [0.0] * (EMBEDDING_DIMENSION - 1),
    ],
)
def test_search_rejects_invalid_vector_dimensions_or_values(vector) -> None:
    client = QdrantClient(":memory:")
    store = make_store(client)

    with pytest.raises(ValueError):
        store.search(vector)

    client.close()


def test_search_rejects_non_positive_limit() -> None:
    client = QdrantClient(":memory:")
    store = make_store(client)

    with pytest.raises(ValueError, match="positive integer"):
        store.search([0.0] * EMBEDDING_DIMENSION, limit=0)

    client.close()
