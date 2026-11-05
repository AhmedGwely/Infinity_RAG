"""Tests for read-only production retrieval evaluation."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient, models

from app.core.config import Settings
from app.core.schemas import (
    ChunkMetadata,
    DocumentChunk,
    ExtractedTable,
    PageReference,
)
from app.embeddings.embedding_service import EMBEDDING_DIMENSION
from app.vectorstore.qdrant_store import QdrantStore
from scripts import evaluate_retrieval as evaluation


def _chunk(text: str, *, with_table: bool = False) -> DocumentChunk:
    return DocumentChunk(
        text=text,
        metadata=ChunkMetadata(
            document_id="edmi-test",
            document_name="Mk6Es-Mk6ESL User Manual v1.3",
            document_type="pdf",
            page_number=60,
            section_title="LCD Formatting",
            chunk_id=str(uuid4()),
            source_file="manual.pdf",
            ingestion_date=datetime(2026, 11, 4, tzinfo=timezone.utc),
            document_version="1.3",
            section_number="8.1",
            source_pages=(
                PageReference(pdf_page=60, printed_page=38),
            ),
        ),
        tables=(
            (
                ExtractedTable(
                    table_id="table-14",
                    pdf_page=60,
                    printed_page=38,
                    caption="LCD unit examples",
                    column_headers=("MODBUS Register", "Description"),
                    rows=(("E000", "PhaseAVoltage"), ("E026", "Powerfactor")),
                    markdown=(
                        "| MODBUS Register | Description |\n"
                        "| E000 | PhaseAVoltage |\n"
                        "| E026 | Powerfactor |"
                    ),
                ),
            )
            if with_table
            else ()
        ),
    )


class FixedEmbedding:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)


def _client_with_points() -> QdrantClient:
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=evaluation.PRODUCTION_COLLECTION,
        vectors_config=models.VectorParams(
            size=EMBEDDING_DIMENSION,
            distance=models.Distance.COSINE,
        ),
    )
    chunks = [
        _chunk(
            "Power Factor register with Unit of Q indicates lead or lag as the unit.",
            with_table=True,
        ),
        _chunk(
            "MODBUS RTU extended feature code 503 provides read access only over "
            "RS-232, RS-485 and Ethernet.",
            with_table=True,
        ),
        _chunk(
            "Modem Mode, Last Occurred Error and Network Info diagnose modem status; "
            "PPP connection established appears when connected."
        ),
        _chunk(
            "2-VT, 2-CT is 3-Phase 3-Wire; 3-VT, 3-CT is 3-Phase 4-Wire."
        ),
        _chunk(
            "DNP3 extended feature code 27 allows registers to be read out over "
            "RS-232 or RS-485."
        ),
    ]
    QdrantStore(
        Settings(collection_name=evaluation.PRODUCTION_COLLECTION),
        client=client,
        create_if_missing=False,
    ).upsert(
        chunks,
        [
            [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)
            for _ in chunks
        ],
    )
    return client


def test_evaluation_uses_retriever_reports_coverage_and_keeps_count_read_only() -> None:
    client = _client_with_points()
    initial_count = client.count(
        evaluation.PRODUCTION_COLLECTION,
        exact=True,
    ).count
    upsert = Mock(wraps=client.upsert)
    create_collection = Mock(wraps=client.create_collection)
    delete_collection = Mock(wraps=client.delete_collection)
    client.upsert = upsert
    client.create_collection = create_collection
    client.delete_collection = delete_collection
    embeddings = FixedEmbedding()

    report = evaluation.evaluate_retrieval(
        settings=Settings(collection_name=evaluation.PRODUCTION_COLLECTION),
        client=client,
        embeddings=embeddings,
    )

    assert len(report.results) == 8
    assert len(embeddings.queries) == 8
    assert report.top_k == 5
    assert all(len(result.hits) == 5 for result in report.results)
    assert report.point_count_unchanged
    assert report.initial_point_count == report.final_point_count == initial_count
    assert all(result.source_page_metadata_exists for result in report.results)
    assert all(result.physical_page_provenance_valid for result in report.results)
    assert report.results[0].table_retrieved
    assert report.results[0].keyword_coverage == 1.0
    assert report.results[0].relevant_result_found
    assert report.results[1].table_support_found
    assert report.results[7].table_support_found
    assert report.results[5].shared_result_count == 5
    upsert.assert_not_called()
    create_collection.assert_not_called()
    delete_collection.assert_not_called()
    client.close()


def test_evaluation_rejects_missing_collection_without_creating_it() -> None:
    client = QdrantClient(":memory:")

    with pytest.raises(RuntimeError, match="will not create it"):
        evaluation.evaluate_retrieval(
            settings=Settings(collection_name=evaluation.PRODUCTION_COLLECTION),
            client=client,
            embeddings=FixedEmbedding(),
        )

    assert client.get_collections().collections == []
    client.close()


@pytest.mark.parametrize("top_k", [0, -1, True])
def test_evaluation_rejects_invalid_top_k(top_k: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        evaluation.evaluate_retrieval(
            settings=Settings(collection_name=evaluation.PRODUCTION_COLLECTION),
            client=QdrantClient(":memory:"),
            embeddings=FixedEmbedding(),
            top_k=top_k,
        )


def test_evaluation_rejects_non_production_collection() -> None:
    with pytest.raises(RuntimeError, match="collection must be"):
        evaluation.evaluate_retrieval(
            settings=Settings(collection_name="some-other-collection"),
            client=QdrantClient(":memory:"),
            embeddings=FixedEmbedding(),
        )
