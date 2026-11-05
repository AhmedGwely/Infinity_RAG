"""Qdrant vector store adapter."""

from datetime import datetime
import math
from typing import Protocol
from uuid import UUID

from qdrant_client import QdrantClient, models

from app.core.config import Settings, load_settings
from app.core.schemas import (
    ChunkMetadata,
    DocumentChunk,
    ExtractedTable,
    PageReference,
    SearchHit,
)
from app.embeddings.embedding_service import EMBEDDING_DIMENSION


class VectorStore(Protocol):
    """Interface for a persistent vector database."""

    def upsert(self, chunks: list[DocumentChunk], vectors: list[list[float]]) -> None:
        """Store chunk vectors and payload metadata."""
        ...

    def search(self, vector: list[float], limit: int) -> list[SearchHit]:
        """Return nearest chunks with their original provenance."""
        ...


class QdrantStore:
    """Persist chunks and their provenance in a cosine-distance Qdrant collection."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: QdrantClient | None = None,
        create_if_missing: bool = True,
    ) -> None:
        self.settings = settings or load_settings()
        self.collection_name = self.settings.collection_name
        self._client = (
            client
            if client is not None
            else QdrantClient(
                url=self.settings.qdrant_url,
                api_key=self.settings.qdrant_api_key,
            )
        )
        if not any(
            collection.name == self.collection_name
            for collection in self._client.get_collections().collections
        ):
            if not create_if_missing:
                raise RuntimeError(
                    f"Qdrant collection {self.collection_name!r} does not exist"
                )
            self._client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=EMBEDDING_DIMENSION,
                    distance=models.Distance.COSINE,
                ),
            )

    def upsert(self, chunks: list[DocumentChunk], vectors: list[list[float]]) -> None:
        """Persist vectors and chunk payloads in the configured collection."""
        if not isinstance(chunks, list) or not isinstance(vectors, list):
            raise TypeError("chunks and vectors must be lists")
        if len(chunks) != len(vectors):
            raise ValueError("chunk and vector counts must match")
        if not chunks:
            return

        points: list[models.PointStruct] = []
        for chunk, vector in zip(chunks, vectors):
            self._validate_chunk(chunk)
            self._validate_vector(vector)
            try:
                point_id = str(UUID(chunk.metadata.chunk_id))
            except (TypeError, ValueError, AttributeError) as error:
                raise ValueError("chunk_id must be a UUID supported by Qdrant") from error
            points.append(
                models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=self._serialize_chunk(chunk),
                )
            )
        self._client.upsert(collection_name=self.collection_name, points=points)

    def search(self, vector: list[float], limit: int = 5) -> list[SearchHit]:
        """Search the configured collection and reconstruct typed search hits."""
        self._validate_vector(vector)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        response = self._client.query_points(
            collection_name=self.collection_name,
            query=vector,
            limit=limit,
            with_payload=True,
        )
        return [
            SearchHit(
                chunk=self._deserialize_chunk(point.payload),
                score=float(point.score),
            )
            for point in response.points
        ]

    @staticmethod
    def _validate_chunk(chunk: DocumentChunk) -> None:
        if not isinstance(chunk, DocumentChunk):
            raise TypeError("each chunk must be a DocumentChunk")
        if not isinstance(chunk.text, str) or not chunk.text.strip():
            raise ValueError("chunk text must be a non-empty string")
        if not isinstance(chunk.metadata, ChunkMetadata):
            raise TypeError("chunk metadata must be ChunkMetadata")
        if not isinstance(chunk.metadata.ingestion_date, datetime):
            raise TypeError("chunk ingestion_date must be a datetime")
        if any(not isinstance(page, PageReference) for page in chunk.metadata.source_pages):
            raise TypeError("source_pages must contain PageReference values")
        if any(not isinstance(table, ExtractedTable) for table in chunk.tables):
            raise TypeError("tables must contain ExtractedTable values")

    @staticmethod
    def _validate_vector(vector: list[float]) -> None:
        if not isinstance(vector, list):
            raise TypeError("vector must be a list of floats")
        if len(vector) != EMBEDDING_DIMENSION:
            raise ValueError(
                f"vector must have {EMBEDDING_DIMENSION} dimensions; received {len(vector)}"
            )
        for value in vector:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("vector values must be finite numbers")
            try:
                finite = math.isfinite(value)
            except OverflowError as error:
                raise ValueError("vector values must be finite numbers") from error
            if not finite:
                raise ValueError("vector values must be finite numbers")

    @staticmethod
    def _serialize_chunk(chunk: DocumentChunk) -> dict[str, object]:
        metadata = chunk.metadata
        return {
            "text": chunk.text,
            "metadata": {
                "document_id": metadata.document_id,
                "document_name": metadata.document_name,
                "document_type": metadata.document_type,
                "page_number": metadata.page_number,
                "section_title": metadata.section_title,
                "chunk_id": metadata.chunk_id,
                "source_file": metadata.source_file,
                "ingestion_date": metadata.ingestion_date.isoformat(),
                "document_version": metadata.document_version,
                "section_number": metadata.section_number,
                "source_pages": [
                    {"pdf_page": page.pdf_page, "printed_page": page.printed_page}
                    for page in metadata.source_pages
                ],
            },
            "tables": [
                {
                    "table_id": table.table_id,
                    "pdf_page": table.pdf_page,
                    "printed_page": table.printed_page,
                    "caption": table.caption,
                    "column_headers": list(table.column_headers),
                    "rows": [list(row) for row in table.rows],
                    "markdown": table.markdown,
                    "section_number": table.section_number,
                    "section_title": table.section_title,
                    "needs_review": table.needs_review,
                }
                for table in chunk.tables
            ],
        }

    @staticmethod
    def _deserialize_chunk(payload: object) -> DocumentChunk:
        if not isinstance(payload, dict):
            raise ValueError("Qdrant result is missing a valid payload")
        try:
            metadata_payload = payload["metadata"]
            if not isinstance(metadata_payload, dict):
                raise TypeError("metadata payload must be an object")
            metadata = ChunkMetadata(
                document_id=metadata_payload["document_id"],
                document_name=metadata_payload["document_name"],
                document_type=metadata_payload["document_type"],
                page_number=metadata_payload["page_number"],
                section_title=metadata_payload["section_title"],
                chunk_id=metadata_payload["chunk_id"],
                source_file=metadata_payload["source_file"],
                ingestion_date=datetime.fromisoformat(metadata_payload["ingestion_date"]),
                document_version=metadata_payload["document_version"],
                section_number=metadata_payload["section_number"],
                source_pages=tuple(
                    PageReference(
                        pdf_page=page["pdf_page"],
                        printed_page=page["printed_page"],
                    )
                    for page in metadata_payload["source_pages"]
                ),
            )
            tables = tuple(
                ExtractedTable(
                    table_id=table["table_id"],
                    pdf_page=table["pdf_page"],
                    printed_page=table["printed_page"],
                    caption=table["caption"],
                    column_headers=tuple(table["column_headers"]),
                    rows=tuple(tuple(row) for row in table["rows"]),
                    markdown=table["markdown"],
                    section_number=table["section_number"],
                    section_title=table["section_title"],
                    needs_review=table["needs_review"],
                )
                for table in payload["tables"]
            )
            return DocumentChunk(text=payload["text"], metadata=metadata, tables=tables)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Qdrant result contains an invalid chunk payload") from error
