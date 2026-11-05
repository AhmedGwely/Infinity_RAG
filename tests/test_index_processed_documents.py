"""Focused tests for safe, idempotent indexing of processed EDMI chunks."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient, models

from app.core.config import Settings
from app.core.schemas import ChunkMetadata, DocumentChunk, PageReference
from app.embeddings.embedding_service import BGEEmbeddingService, EMBEDDING_DIMENSION
from app.vectorstore.qdrant_store import QdrantStore
from scripts import index_processed_documents as indexing


INGESTION_DATE = "2026-11-04T09:05:37.650016+00:00"
DOCUMENT = {
    "document_id": "processed-edmi-document-id",
    "document_name": indexing.EXPECTED_DOCUMENT_NAME,
    "document_version": "1.3",
    "source_file": r"D:\manuals\Mk6Es-Mk6ESL User Manual v1.3.pdf",
    "page_count": 179,
    "ingestion_date": INGESTION_DATE,
}


def _chunk_data(index: int) -> dict[str, Any]:
    page_number = index + 1
    printed_page = 12 if index == 1 else None
    return {
        "text": f"Processed EDMI manual text for page {page_number}.",
        "metadata": {
            "document_id": DOCUMENT["document_id"],
            "document_name": DOCUMENT["document_name"],
            "document_type": "pdf",
            "page_number": page_number,
            "section_title": f"Section {page_number}",
            "chunk_id": str(uuid4()),
            "source_file": DOCUMENT["source_file"],
            "ingestion_date": INGESTION_DATE,
            "document_version": DOCUMENT["document_version"],
            "section_number": str(page_number),
            "source_pages": [
                {"pdf_page": page_number, "printed_page": printed_page}
            ],
        },
        "tables": (
            [
                {
                    "table_id": "table-1",
                    "pdf_page": page_number,
                    "printed_page": printed_page,
                    "caption": "Example table",
                    "column_headers": ["Register", "Value"],
                    "rows": [["0x1000", "230 V"]],
                    "markdown": "| Register | Value |",
                    "section_number": str(page_number),
                    "section_title": f"Section {page_number}",
                    "needs_review": False,
                }
            ]
            if index == 2
            else []
        ),
    }


def _processed_file(path: Path, chunk_count: int = 3) -> Path:
    data = {
        "document": DOCUMENT,
        "chunks": [_chunk_data(index) for index in range(chunk_count)],
        "quality_report": {},
    }
    processed_path = path / "processed-edmi.json"
    processed_path.write_text(json.dumps(data), encoding="utf-8")
    return processed_path


def _settings() -> Settings:
    return Settings(collection_name=indexing.PRODUCTION_COLLECTION)


def _client(*, dimensions: int = EMBEDDING_DIMENSION) -> QdrantClient:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=indexing.PRODUCTION_COLLECTION,
        vectors_config=models.VectorParams(
            size=dimensions,
            distance=models.Distance.COSINE,
        ),
    )
    return client


def _embedding_service() -> BGEEmbeddingService:
    class Model:
        def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
            assert kwargs["batch_size"] == indexing.EMBEDDING_BATCH_SIZE
            assert kwargs["normalize_embeddings"] is True
            return [[1.0] + [0.0] * (EMBEDDING_DIMENSION - 1) for _ in texts]

    return BGEEmbeddingService(
        model_name="test-model",
        batch_size=indexing.EMBEDDING_BATCH_SIZE,
        model=Model(),
    )


def test_indexing_upserts_idempotently_and_verifies_payload_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processed_path = _processed_file(tmp_path)
    client = _client()
    created_services: list[dict[str, Any]] = []

    def create_embedding_service(**kwargs: Any) -> BGEEmbeddingService:
        created_services.append(kwargs)
        return _embedding_service()

    monkeypatch.setattr(indexing, "BGEEmbeddingService", create_embedding_service)

    first = indexing.index_processed_documents(
        settings=_settings(),
        client=client,
        processed_path=processed_path,
    )
    second = indexing.index_processed_documents(
        settings=_settings(),
        client=client,
        processed_path=processed_path,
    )

    assert first.loaded_chunks == first.vectors_generated == 3
    assert first.inserted_points == 3
    assert first.updated_points == 0
    assert second.inserted_points == 0
    assert second.updated_points == 3
    assert first.final_point_count == second.final_point_count == 3
    assert first.metadata_verified
    assert first.physical_pages_verified
    assert first.printed_pages_verified
    assert first.verified_sample_count == 3
    assert all(
        service["batch_size"] == indexing.EMBEDDING_BATCH_SIZE
        and service["model_name"] == _settings().embedding_model
        for service in created_services
    )
    assert client.count(indexing.PRODUCTION_COLLECTION, exact=True).count == 3
    client.close()


def test_validation_rejects_empty_chunks_and_invalid_ids(tmp_path: Path) -> None:
    path = _processed_file(tmp_path)
    document, chunks = indexing._load_manual(path)

    with pytest.raises(ValueError, match="contains no chunks"):
        indexing._validate_document_chunks([], document)

    invalid_id_chunk = replace(
        chunks[0],
        metadata=replace(chunks[0].metadata, chunk_id="not-a-uuid"),
    )
    with pytest.raises(ValueError, match="Qdrant cannot represent"):
        indexing._validate_document_chunks([invalid_id_chunk], document)


@pytest.mark.parametrize(
    ("chunk_change", "message"),
    [
        (lambda chunk: replace(chunk, text=" \n"), "empty text"),
        (
            lambda chunk: replace(
                chunk, metadata=replace(chunk.metadata, document_name="Other manual")
            ),
            "mismatched document_name",
        ),
        (
            lambda chunk: replace(
                chunk,
                metadata=replace(
                    chunk.metadata,
                    source_pages=(PageReference(pdf_page=0, printed_page=None),),
                ),
            ),
            "invalid physical PDF page",
        ),
    ],
)
def test_validation_rejects_bad_content_metadata_and_provenance(
    tmp_path: Path,
    chunk_change,
    message: str,
) -> None:
    path = _processed_file(tmp_path)
    document, chunks = indexing._load_manual(path)

    with pytest.raises(ValueError, match=message):
        indexing._validate_document_chunks([chunk_change(chunks[0])], document)


def test_validation_rejects_duplicate_qdrant_ids(tmp_path: Path) -> None:
    path = _processed_file(tmp_path)
    document, chunks = indexing._load_manual(path)
    duplicate = replace(
        chunks[1],
        metadata=replace(chunks[1].metadata, chunk_id=chunks[0].metadata.chunk_id),
    )

    with pytest.raises(ValueError, match="duplicate Qdrant chunk ID"):
        indexing._validate_document_chunks([chunks[0], duplicate], document)


def test_incompatible_collection_fails_before_embedding(
    tmp_path: Path,
) -> None:
    processed_path = _processed_file(tmp_path)
    client = _client(dimensions=768)
    model = Mock()
    embedder = BGEEmbeddingService(
        model_name="test-model",
        batch_size=indexing.EMBEDDING_BATCH_SIZE,
        model=model,
    )

    with pytest.raises(RuntimeError, match="incompatible"):
        indexing.index_processed_documents(
            settings=_settings(),
            client=client,
            embedding_service=embedder,
            processed_path=processed_path,
        )

    model.encode.assert_not_called()
    assert client.count(indexing.PRODUCTION_COLLECTION, exact=True).count == 0
    client.close()


def test_existing_id_owned_by_another_document_is_never_overwritten(
    tmp_path: Path,
) -> None:
    processed_path = _processed_file(tmp_path)
    _document, chunks = indexing._load_manual(processed_path)
    client = _client()
    first = chunks[0]
    foreign_chunk = DocumentChunk(
        text=first.text,
        metadata=replace(first.metadata, document_id="another-document"),
        tables=first.tables,
    )
    QdrantStore(_settings(), client=client).upsert(
        [foreign_chunk],
        [[1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)],
    )

    class UnusedEmbedder:
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            pytest.fail("embedding must not start when an ID collision is detected")

    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        indexing.index_processed_documents(
            settings=_settings(),
            client=client,
            embedding_service=UnusedEmbedder(),  # type: ignore[arg-type]
            processed_path=processed_path,
        )

    assert client.count(indexing.PRODUCTION_COLLECTION, exact=True).count == 1
    client.close()


def test_missing_collection_is_not_created(tmp_path: Path) -> None:
    processed_path = _processed_file(tmp_path)
    client = QdrantClient(":memory:")

    with pytest.raises(RuntimeError, match="refusing to create it"):
        indexing.index_processed_documents(
            settings=_settings(),
            client=client,
            embedding_service=_embedding_service(),
            processed_path=processed_path,
        )

    assert client.get_collections().collections == []
    client.close()
