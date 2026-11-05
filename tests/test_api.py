"""Mocked API integration tests for grounded /query responses."""

from datetime import datetime, timezone
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.schemas import (
    ChunkMetadata,
    DocumentChunk,
    PageReference,
    SearchHit,
)
from app.generation.answer_generator import AnswerGenerator, NOT_FOUND_ANSWER
from app.generation.providers import GeminiProvider
from app.api.routes import create_app


def _hit(
    *,
    document_id: str = "doc-1",
    document_name: str = "Mk6ES Manual",
    chunk_id: str = "chunk-1",
    pages: tuple[PageReference, ...] = (PageReference(12, 8),),
    page_number: int = 12,
) -> SearchHit:
    metadata = ChunkMetadata(
        document_id=document_id,
        document_name=document_name,
        document_type="pdf",
        page_number=page_number,
        section_title="Communications",
        chunk_id=chunk_id,
        source_file="manual.pdf",
        ingestion_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        section_number="4.2",
        source_pages=pages,
    )
    return SearchHit(
        chunk=DocumentChunk(
            text=f"Engineering evidence for {document_id}.",
            metadata=metadata,
        ),
        score=0.9,
    )


def _client(hits, answer_text: str, *, generator_error: Exception | None = None):
    retriever = Mock()
    retriever.retrieve.return_value = hits
    gemini_client = Mock()
    if generator_error is None:
        gemini_client.interactions.create.return_value.output_text = answer_text
    else:
        gemini_client.interactions.create.side_effect = generator_error
    generator = AnswerGenerator(
        GeminiProvider(
            api_key="test-key-never-exposed",
            model="gemini-3.8-flash",
            client=gemini_client,
        )
    )
    app = create_app(
        settings=Settings(llm_provider="gemini"),
        retriever=retriever,
        answer_generator=generator,
    )
    return TestClient(app), retriever, gemini_client


def test_query_returns_grounded_answer_and_structured_sources() -> None:
    hits = [
        _hit(
            pages=(
                PageReference(pdf_page=12, printed_page=None),
                PageReference(pdf_page=13, printed_page=9),
            )
        ),
        _hit(
            document_id="doc-2",
            document_name="Communication Supplement",
            chunk_id="chunk-2",
            pages=(PageReference(pdf_page=42, printed_page=37),),
            page_number=42,
        ),
    ]
    client, retriever, gemini_client = _client(
        hits,
        "The meter reports the negotiated PPP session state [S1]. "
        "The register is described in the supplement [S2].",
    )

    with client:
        response = client.post(
            "/query",
            json={"question": "What does the meter report during PPP connectivity?"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"].startswith("The meter reports the negotiated PPP session state [S1].")
    assert len(body["sources"]) == 2
    first_source = body["sources"][0]
    assert first_source == {
        "citation_id": "S1",
        "document_name": "Mk6ES Manual",
        "document_id": "doc-1",
        "passage": "Engineering evidence for doc-1.",
        "pdf_pages": [12, 13],
        "printed_pages": [9],
        "source_pages": [
            {"pdf_page": 12, "printed_page": None},
            {"pdf_page": 13, "printed_page": 9},
        ],
        "section_number": "4.2",
        "section_title": "Communications",
        "chunk_id": "chunk-1",
    }
    assert body["sources"][1]["document_id"] == "doc-2"
    retriever.retrieve.assert_called_once_with(
        "What does the meter report during PPP connectivity?"
    )
    request = gemini_client.interactions.create.call_args.kwargs
    assert request["model"] == "gemini-3.8-flash"
    assert request["store"] is False
    assert "Engineering evidence for doc-1." in request["input"]
    assert "Engineering evidence for doc-2." in request["input"]
    assert "test-key-never-exposed" not in response.text


def test_query_rejects_empty_or_whitespace_question() -> None:
    client, retriever, gemini_client = _client([], "irrelevant")
    with client:
        empty_response = client.post("/query", json={"question": ""})
        whitespace_response = client.post("/query", json={"question": "   "})

    assert empty_response.status_code == 422
    assert whitespace_response.status_code == 422
    retriever.retrieve.assert_not_called()
    gemini_client.interactions.create.assert_not_called()


def test_query_with_no_retrieved_documents_abstains_without_gemini() -> None:
    client, _, gemini_client = _client([], "unused")
    with client:
        response = client.post("/query", json={"question": "Unknown register?"})

    assert response.status_code == 200
    assert response.json() == {"answer": NOT_FOUND_ANSWER, "sources": []}
    gemini_client.interactions.create.assert_not_called()


def test_query_handles_missing_printed_page_without_inventing_it() -> None:
    hit = _hit(
        pages=(PageReference(pdf_page=22, printed_page=None),),
        page_number=22,
    )
    client, _, _ = _client([hit], "The manual describes the setting [S1].")
    with client:
        response = client.post("/query", json={"question": "What setting is described?"})

    source = response.json()["sources"][0]
    assert source["pdf_pages"] == [22]
    assert source["printed_pages"] == []
    assert source["source_pages"] == [{"pdf_page": 22, "printed_page": None}]


def test_query_rejects_invalid_citation_marker_safely() -> None:
    client, _, _ = _client([_hit()], "Unsupported statement [S2].")
    with client:
        response = client.post("/query", json={"question": "What does the manual say?"})

    assert response.status_code == 502
    assert "citation" in response.json()["detail"].lower()
    assert "S2" not in response.text


def test_api_uses_shared_parser_to_reject_malformed_markers() -> None:
    retriever = Mock()
    retriever.retrieve.return_value = [_hit()]
    generator = Mock()
    generator.answer.return_value = "Supported text [S1], with malformed marker [S]."
    app = create_app(
        settings=Settings(llm_provider="gemini"),
        retriever=retriever,
        answer_generator=generator,
    )
    with TestClient(app) as client:
        response = client.post("/query", json={"question": "What is supported?"})

    assert response.status_code == 502
    assert "citation" in response.json()["detail"].lower()


def test_query_handles_gemini_failure_without_exposing_provider_details() -> None:
    client, _, _ = _client(
        [_hit()],
        "",
        generator_error=RuntimeError("test-key-never-exposed"),
    )
    with client:
        response = client.post("/query", json={"question": "What is documented?"})

    assert response.status_code == 502
    assert "test-key-never-exposed" not in response.text


def test_query_handles_retrieval_failure_without_exposing_internal_details() -> None:
    retriever = Mock()
    retriever.retrieve.side_effect = RuntimeError("sensitive qdrant internals")
    generator = Mock()
    app = create_app(
        settings=Settings(llm_provider="gemini"),
        retriever=retriever,
        answer_generator=generator,
    )
    with TestClient(app) as client:
        response = client.post("/query", json={"question": "Where is the register?"})

    assert response.status_code == 503
    assert "sensitive qdrant internals" not in response.text
    generator.answer.assert_not_called()


def test_query_response_schema_contains_answer_and_sources() -> None:
    client, _, _ = _client([_hit()], "Evidence supports this [S1].")
    with client:
        response = client.post("/query", json={"question": "What is supported?"})

    assert response.status_code == 200
    assert set(response.json()) == {"answer", "sources"}
    assert response.json()["sources"][0]["passage"] == "Engineering evidence for doc-1."
    assert set(response.json()["sources"][0]) == {
        "citation_id",
        "document_name",
        "document_id",
        "pdf_pages",
        "printed_pages",
        "source_pages",
        "section_number",
        "section_title",
        "chunk_id",
        "passage",
    }
