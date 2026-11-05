"""Persist already-processed EDMI manual chunks in the production Qdrant collection."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qdrant_client import QdrantClient, models

from app.core.config import Settings, load_settings
from app.core.schemas import DocumentChunk, PageReference
from app.embeddings.embedding_service import BGEEmbeddingService, EMBEDDING_DIMENSION
from app.vectorstore.qdrant_store import QdrantStore
from scripts.verify_edmi_rag import _load_processed_chunks, _processed_manual_path


PRODUCTION_COLLECTION = "rd_engineering"
EXPECTED_DOCUMENT_NAME = "Mk6Es-Mk6ESL User Manual v1.3"
EMBEDDING_BATCH_SIZE = 4
RETRIEVE_BATCH_SIZE = 128


@dataclass(frozen=True)
class IndexingResult:
    """Counts and verification outcomes from one successful indexing run."""

    loaded_chunks: int
    vectors_generated: int
    inserted_points: int
    updated_points: int
    final_point_count: int
    metadata_verified: bool
    physical_pages_verified: bool
    printed_pages_verified: bool
    verified_sample_count: int


def _validate_document_chunks(
    chunks: list[DocumentChunk], document: dict[str, Any]
) -> list[str]:
    """Validate the processed manual's identity, chunk IDs, and page provenance."""
    if not chunks:
        raise ValueError("the processed EDMI manual contains no chunks")

    document_id = document.get("document_id")
    document_name = document.get("document_name")
    document_version = document.get("document_version")
    source_file = document.get("source_file")
    page_count = document.get("page_count")
    ingestion_date_value = document.get("ingestion_date")
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("processed document is missing a valid document_id")
    if document_name != EXPECTED_DOCUMENT_NAME:
        raise ValueError(
            f"processed document name must be {EXPECTED_DOCUMENT_NAME!r}"
        )
    if document_version != "1.3":
        raise ValueError("processed EDMI document version must be '1.3'")
    if not isinstance(source_file, str) or not source_file.lower().endswith(".pdf"):
        raise ValueError("processed document is missing a valid PDF source_file")
    if (
        not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or page_count <= 0
    ):
        raise ValueError("processed document is missing a valid page_count")
    try:
        ingestion_date = datetime.fromisoformat(ingestion_date_value)
    except (TypeError, ValueError) as error:
        raise ValueError("processed document has an invalid ingestion_date") from error

    point_ids: list[str] = []
    seen_ids: set[str] = set()
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, DocumentChunk):
            raise TypeError(f"chunk {index} is not a DocumentChunk")
        if not isinstance(chunk.text, str) or not chunk.text.strip():
            raise ValueError(f"chunk {index} has empty text")

        metadata = chunk.metadata
        if metadata.document_id != document_id:
            raise ValueError(f"chunk {index} has a mismatched document_id")
        if metadata.document_name != document_name:
            raise ValueError(f"chunk {index} has a mismatched document_name")
        if metadata.document_version != document_version:
            raise ValueError(f"chunk {index} has a mismatched document_version")
        if metadata.source_file != source_file:
            raise ValueError(f"chunk {index} has a mismatched source_file")
        if metadata.document_type != "pdf":
            raise ValueError(f"chunk {index} has an unexpected document_type")
        if metadata.ingestion_date != ingestion_date:
            raise ValueError(f"chunk {index} has a mismatched ingestion_date")

        try:
            point_id = str(UUID(metadata.chunk_id))
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError(
                f"chunk {index} has an ID that Qdrant cannot represent"
            ) from error
        if point_id in seen_ids:
            raise ValueError(f"duplicate Qdrant chunk ID: {point_id}")
        seen_ids.add(point_id)
        point_ids.append(point_id)

        page_number = metadata.page_number
        if (
            not isinstance(page_number, int)
            or isinstance(page_number, bool)
            or not 1 <= page_number <= page_count
        ):
            raise ValueError(f"chunk {index} has an invalid physical page number")
        if not metadata.source_pages:
            raise ValueError(f"chunk {index} has no physical page provenance")

        for page in metadata.source_pages:
            if not isinstance(page, PageReference):
                raise ValueError(f"chunk {index} has an invalid source page reference")
            if (
                not isinstance(page.pdf_page, int)
                or isinstance(page.pdf_page, bool)
                or not 1 <= page.pdf_page <= page_count
            ):
                raise ValueError(f"chunk {index} has an invalid physical PDF page")
            if page.printed_page is not None and (
                not isinstance(page.printed_page, int)
                or isinstance(page.printed_page, bool)
                or page.printed_page <= 0
            ):
                raise ValueError(f"chunk {index} has an invalid printed page number")
        if metadata.page_number != metadata.source_pages[0].pdf_page:
            raise ValueError(f"chunk {index} page_number does not match its provenance")

        source_pdf_pages = {page.pdf_page for page in metadata.source_pages}
        for table in chunk.tables:
            if (
                not isinstance(table.pdf_page, int)
                or isinstance(table.pdf_page, bool)
                or not 1 <= table.pdf_page <= page_count
            ):
                raise ValueError(f"chunk {index} has a table with an invalid PDF page")
            if table.pdf_page not in source_pdf_pages:
                raise ValueError(
                    f"chunk {index} has a table without matching page provenance"
                )
            if table.printed_page is not None and (
                not isinstance(table.printed_page, int)
                or isinstance(table.printed_page, bool)
                or table.printed_page <= 0
            ):
                raise ValueError(f"chunk {index} has a table with an invalid printed page")

    return point_ids


def _load_manual(path: Path) -> tuple[dict[str, Any], list[DocumentChunk]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"could not read processed JSON {path.name}") from error
    document = data.get("document") if isinstance(data, dict) else None
    if not isinstance(document, dict):
        raise ValueError("processed JSON is missing its document metadata")
    chunks, _quality_report = _load_processed_chunks(path)
    return document, chunks


def _validate_vector_configuration(collection: Any) -> None:
    vector_config = collection.config.params.vectors
    if not isinstance(vector_config, models.VectorParams):
        raise RuntimeError(
            "production collection must use one unnamed dense vector configuration"
        )
    if (
        vector_config.size != EMBEDDING_DIMENSION
        or vector_config.distance != models.Distance.COSINE
    ):
        raise RuntimeError(
            "production collection vector configuration is incompatible; expected "
            f"{EMBEDDING_DIMENSION}-dimensional cosine vectors"
        )


def _existing_points(
    client: QdrantClient,
    point_ids: list[str],
    document: dict[str, Any],
) -> set[str]:
    existing_ids: set[str] = set()
    for start in range(0, len(point_ids), RETRIEVE_BATCH_SIZE):
        points = client.retrieve(
            collection_name=PRODUCTION_COLLECTION,
            ids=point_ids[start : start + RETRIEVE_BATCH_SIZE],
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            point_id = str(UUID(str(point.id)))
            payload = point.payload
            metadata = payload.get("metadata") if isinstance(payload, dict) else None
            if not isinstance(metadata, dict):
                raise RuntimeError(
                    f"existing point {point_id} has no verifiable document metadata"
                )
            if (
                metadata.get("document_id") != document["document_id"]
                or metadata.get("document_name") != document["document_name"]
                or metadata.get("document_version") != document["document_version"]
            ):
                raise RuntimeError(
                    f"refusing to overwrite Qdrant point {point_id} owned by another "
                    "document or version"
                )
            existing_ids.add(point_id)
    return existing_ids


def _retrieve_and_verify(
    client: QdrantClient,
    chunks: list[DocumentChunk],
    point_ids: list[str],
) -> tuple[int, bool, bool, bool]:
    retrieved: dict[str, Any] = {}
    for start in range(0, len(point_ids), RETRIEVE_BATCH_SIZE):
        points = client.retrieve(
            collection_name=PRODUCTION_COLLECTION,
            ids=point_ids[start : start + RETRIEVE_BATCH_SIZE],
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            retrieved[str(UUID(str(point.id)))] = point

    if len(retrieved) != len(point_ids):
        missing = len(point_ids) - len(retrieved)
        raise RuntimeError(
            f"post-upsert verification found {missing} missing or duplicate points"
        )

    by_id = dict(zip(point_ids, chunks))
    for point_id, expected_chunk in by_id.items():
        point = retrieved.get(point_id)
        if point is None or point.payload is None:
            raise RuntimeError(f"post-upsert verification missing point {point_id}")
        actual_chunk = QdrantStore._deserialize_chunk(point.payload)
        if actual_chunk != expected_chunk:
            raise RuntimeError(
                f"post-upsert metadata, text, table, or provenance mismatch for "
                f"chunk {point_id}"
            )

    physical_pages_verified = all(
        bool(chunk.metadata.source_pages) for chunk in chunks
    )
    printed_pages_available = any(
        page.printed_page is not None
        for chunk in chunks
        for page in chunk.metadata.source_pages
    )
    printed_pages_verified = (
        not printed_pages_available
        or all(
            isinstance(page.printed_page, int)
            for chunk in chunks
            for page in chunk.metadata.source_pages
            if page.printed_page is not None
        )
    )
    if not physical_pages_verified or not printed_pages_verified:
        raise RuntimeError("post-upsert page provenance verification failed")

    return (
        len(retrieved),
        True,
        physical_pages_verified,
        printed_pages_verified,
    )


def index_processed_documents(
    *,
    settings: Settings | None = None,
    client: QdrantClient | None = None,
    embedding_service: BGEEmbeddingService | None = None,
    processed_path: Path | None = None,
) -> IndexingResult:
    """Validate, embed, upsert, and verify processed EDMI manual chunks."""
    settings = settings or load_settings()
    if settings.collection_name != PRODUCTION_COLLECTION:
        raise RuntimeError(
            f"refusing to index: configured collection must be "
            f"{PRODUCTION_COLLECTION!r}"
        )

    path = processed_path or _processed_manual_path()
    document, chunks = _load_manual(path)
    point_ids = _validate_document_chunks(chunks, document)
    print(f"Loaded {len(chunks)} processed chunks from {path.name}.")

    owns_client = client is None
    qdrant = (
        client
        if client is not None
        else QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    )
    try:
        collections = {
            item.name for item in qdrant.get_collections().collections
        }
        if PRODUCTION_COLLECTION not in collections:
            raise RuntimeError(
                f"required production collection {PRODUCTION_COLLECTION!r} does not exist; "
                "refusing to create it"
            )

        collection_count_before = qdrant.count(
            collection_name=PRODUCTION_COLLECTION,
            exact=True,
        ).count
        print(
            f"Existing point count in {PRODUCTION_COLLECTION}: "
            f"{collection_count_before}."
        )
        _validate_vector_configuration(qdrant.get_collection(PRODUCTION_COLLECTION))
        print(
            f"Collection vector configuration verified "
            f"({EMBEDDING_DIMENSION} dimensions, cosine)."
        )

        existing_ids = _existing_points(qdrant, point_ids, document)
        missing_ids = len(point_ids) - len(existing_ids)

        embeddings = embedding_service or BGEEmbeddingService(
            model_name=settings.embedding_model,
            device="cpu",
            batch_size=EMBEDDING_BATCH_SIZE,
            settings=settings,
        )
        vectors = embeddings.embed_documents([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise RuntimeError("embedding count did not match processed chunk count")
        for index, vector in enumerate(vectors):
            if len(vector) != EMBEDDING_DIMENSION:
                raise RuntimeError(
                    f"embedding {index} has {len(vector)} dimensions; expected "
                    f"{EMBEDDING_DIMENSION}"
                )
            if not all(math.isfinite(value) for value in vector):
                raise RuntimeError(f"embedding {index} contains a non-finite value")
            magnitude = math.sqrt(sum(value * value for value in vector))
            if not math.isclose(magnitude, 1.0, rel_tol=1e-4, abs_tol=1e-4):
                raise RuntimeError(f"embedding {index} is not normalized")
        print(f"Generated {len(vectors)} normalized vectors.")

        store = QdrantStore(settings, client=qdrant)
        store.upsert(chunks, vectors)
        print(f"Upserted {len(chunks)} points using stable chunk IDs.")

        verified_count, metadata_verified, physical_verified, printed_verified = (
            _retrieve_and_verify(qdrant, chunks, point_ids)
        )
        final_point_count = qdrant.count(
            collection_name=PRODUCTION_COLLECTION,
            exact=True,
        ).count
        expected_point_count = collection_count_before + missing_ids
        if final_point_count != expected_point_count:
            raise RuntimeError(
                "collection point count mismatch after upsert: expected "
                f"{expected_point_count}, found {final_point_count}"
            )

        sample_indexes = {0, len(chunks) // 2, len(chunks) - 1}
        sample_indexes.update(
            index
            for index, chunk in enumerate(chunks)
            if chunk.tables
        )
        sample_indexes.update(
            index
            for index, chunk in enumerate(chunks)
            if any(page.printed_page is not None for page in chunk.metadata.source_pages)
        )
        sample_count = min(len(sample_indexes), verified_count)
        print(
            f"Verified all {verified_count} expected points, including "
            f"{sample_count} samples; text, metadata, tables, and page references match."
        )
        print(
            f"Final {PRODUCTION_COLLECTION} point count: {final_point_count} "
            f"(expected {expected_point_count})."
        )

        return IndexingResult(
            loaded_chunks=len(chunks),
            vectors_generated=len(vectors),
            inserted_points=missing_ids,
            updated_points=len(existing_ids),
            final_point_count=final_point_count,
            metadata_verified=metadata_verified,
            physical_pages_verified=physical_verified,
            printed_pages_verified=printed_verified,
            verified_sample_count=sample_count,
        )
    finally:
        if owns_client:
            qdrant.close()


def _safe_error(error: Exception, settings: Settings | None) -> str:
    message = str(error)
    if settings is not None:
        for sensitive_value in (settings.qdrant_api_key, settings.qdrant_url):
            if sensitive_value:
                message = message.replace(sensitive_value, "[redacted]")
    return f"{type(error).__name__}: {message}"


def main() -> int:
    """Run indexing with explicit failure reporting and no destructive operations."""
    settings: Settings | None = None
    try:
        settings = load_settings()
        result = index_processed_documents(settings=settings)
    except Exception as error:
        print(
            f"Processed-document indexing failed: {_safe_error(error, settings)}",
            file=sys.stderr,
        )
        return 1

    print(
        "Indexing complete: "
        f"loaded={result.loaded_chunks}, vectors={result.vectors_generated}, "
        f"inserted={result.inserted_points}, updated={result.updated_points}, "
        f"final_points={result.final_point_count}, "
        f"metadata_verified={result.metadata_verified}, "
        f"physical_pages_verified={result.physical_pages_verified}, "
        f"printed_pages_verified={result.printed_pages_verified}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
