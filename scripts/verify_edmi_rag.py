"""Index cached EDMI extraction into a temporary collection and verify retrieval."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qdrant_client import QdrantClient

from app.core.config import load_settings
from app.core.schemas import (
    ChunkMetadata,
    DocumentChunk,
    ExtractedTable,
    PageReference,
)
from app.embeddings.embedding_service import BGEEmbeddingService, EMBEDDING_DIMENSION
from app.retrieval.retriever import Retriever
from app.vectorstore.qdrant_store import QdrantStore


PRODUCTION_COLLECTION = "rd_engineering"
QUESTIONS = (
    (
        "What is cascade summation, and how are meters connected?",
        ("cascade", "summation"),
    ),
    ("What does the Modem Mode register indicate?", ("modem", "mode")),
    (
        "What does the meter report when the modem establishes a PPP connection?",
        ("ppp", "modem"),
    ),
    (
        "How can the meter's communication status be diagnosed?",
        ("communication", "status"),
    ),
    ("What are the communication-related register references?", ("register", "communication")),
    (
        "How does the meter's billing reset lockout interval work?",
        ("billing", "lockout"),
    ),
)


def _processed_manual_path() -> Path:
    processed_directory = PROJECT_ROOT / "documents" / "processed"
    candidates: list[Path] = []
    for path in processed_directory.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        document = data.get("document")
        if not isinstance(document, dict) or not isinstance(data.get("chunks"), list):
            continue
        if document.get("document_name") == "Mk6Es-Mk6ESL User Manual v1.3":
            candidates.append(path)
    if len(candidates) != 1:
        raise FileNotFoundError(
            "Expected one processed EDMI manual JSON; found "
            f"{len(candidates)} under {processed_directory}."
        )
    return candidates[0]


def _load_processed_chunks(path: Path) -> tuple[list[DocumentChunk], dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    chunks: list[DocumentChunk] = []
    for item in data["chunks"]:
        metadata_data = item["metadata"]
        metadata = ChunkMetadata(
            document_id=metadata_data["document_id"],
            document_name=metadata_data["document_name"],
            document_type=metadata_data["document_type"],
            page_number=metadata_data["page_number"],
            section_title=metadata_data["section_title"],
            chunk_id=metadata_data["chunk_id"],
            source_file=metadata_data["source_file"],
            ingestion_date=datetime.fromisoformat(metadata_data["ingestion_date"]),
            document_version=metadata_data["document_version"],
            section_number=metadata_data["section_number"],
            source_pages=tuple(
                PageReference(
                    pdf_page=page["pdf_page"],
                    printed_page=page["printed_page"],
                )
                for page in metadata_data["source_pages"]
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
            for table in item["tables"]
        )
        chunks.append(
            DocumentChunk(text=item["text"], metadata=metadata, tables=tables)
        )
    return chunks, data["quality_report"]


def _safe_error(error: Exception, api_key: str | None) -> str:
    message = str(error)
    if api_key:
        message = message.replace(api_key, "[redacted]")
    return f"{type(error).__name__}: {message}"


def main() -> int:
    """Run real local embedding and retrieval without touching production data."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    collection_name = f"edmi_verify_{uuid4().hex}"
    if collection_name == PRODUCTION_COLLECTION:
        print("SAFETY FAILURE: refusing to use the production collection.", file=sys.stderr)
        return 1

    client: QdrantClient | None = None
    collection_was_absent = False
    settings = None
    exit_code = 1
    production_count_before: int | None = None
    production_existed_before = False
    try:
        settings = replace(
            load_settings(),
            qdrant_url="http://localhost:6333",
            collection_name=collection_name,
        )

        print("Reading already-processed EDMI manual chunks; the PDF will not be reprocessed.")
        processed_path = _processed_manual_path()
        chunks, quality_report = _load_processed_chunks(processed_path)
        if not chunks:
            raise RuntimeError("The processed EDMI manual contains no chunks.")
        print(
            f"Loaded {len(chunks)} processed chunks from {processed_path.name}. "
            f"Extraction methods: {quality_report.get('extraction_methods')}; "
            f"pages requiring review: {quality_report.get('pages_requiring_review')}."
        )

        print("Connecting to Qdrant at http://localhost:6333 ...", flush=True)
        client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
        )
        existing_names = {
            collection.name for collection in client.get_collections().collections
        }
        production_existed_before = PRODUCTION_COLLECTION in existing_names
        if production_existed_before:
            production_count_before = client.count(
                collection_name=PRODUCTION_COLLECTION,
                exact=True,
            ).count
        if collection_name in existing_names:
            raise RuntimeError("generated temporary collection name already exists")
        collection_was_absent = True
        print("Qdrant connection confirmed; production collection will not be written.", flush=True)

        store = QdrantStore(settings, client=client)
        collection = client.get_collection(collection_name)
        vector_config = collection.config.params.vectors
        if (
            vector_config.size != EMBEDDING_DIMENSION
            or vector_config.distance.name != "COSINE"
        ):
            raise RuntimeError("temporary collection has unexpected vector configuration")

        print("Loading BAAI/bge-m3 on CPU and embedding all processed chunks ...", flush=True)
        embeddings = BGEEmbeddingService(
            model_name="BAAI/bge-m3",
            device="cpu",
            batch_size=4,
        )
        vectors = embeddings.embed_documents([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise RuntimeError("embedding count did not match processed chunks")
        print(f"Generated {len(vectors)} local vectors.", flush=True)

        print("Indexing processed chunks in the temporary Qdrant collection ...", flush=True)
        store.upsert(chunks, vectors)
        print("Temporary collection indexing complete.", flush=True)

        retriever = Retriever(embeddings, store)
        original_by_id = {chunk.metadata.chunk_id: chunk for chunk in chunks}
        printed_provenance_verified = False
        pdf_provenance_verified = False
        queries_with_support = 0
        for question, support_terms in QUESTIONS:
            print(f"\nQuestion: {question}")
            hits = retriever.retrieve(question, limit=5)
            if not hits:
                print("No passages retrieved; supporting evidence was not found.")
                continue
            relevant_text = "\n".join(hit.chunk.text.lower() for hit in hits)
            if all(term in relevant_text for term in support_terms):
                queries_with_support += 1
            else:
                print("No obvious supporting passage in the retrieved results; treat as insufficient.")

            for index, hit in enumerate(hits, start=1):
                metadata = hit.chunk.metadata
                original = original_by_id.get(metadata.chunk_id)
                if original is None:
                    raise RuntimeError("retrieval returned a chunk ID absent from the source extraction")
                if (
                    metadata.document_id != original.metadata.document_id
                    or metadata.document_name != original.metadata.document_name
                    or metadata.page_number != original.metadata.page_number
                    or metadata.source_pages != original.metadata.source_pages
                    or metadata.section_number != original.metadata.section_number
                    or metadata.section_title != original.metadata.section_title
                    or hit.chunk.text != original.text
                ):
                    raise RuntimeError(
                        f"metadata or provenance round-trip failed for chunk {metadata.chunk_id}"
                    )
                pdf_provenance_verified |= bool(metadata.source_pages)
                printed_provenance_verified |= any(
                    page.printed_page is not None for page in metadata.source_pages
                )
                pages = "; ".join(
                    f"PDF {page.pdf_page} (printed "
                    f"{page.printed_page if page.printed_page is not None else 'unavailable'})"
                    for page in metadata.source_pages
                ) or f"PDF {metadata.page_number} (printed unavailable)"
                print(
                    f"\n{index}. score={hit.score:.4f} | document={metadata.document_name} "
                    f"| document_id={metadata.document_id} | pages={pages} "
                    f"| section={metadata.section_number or 'unavailable'} "
                    f"{metadata.section_title or ''} | chunk_id={metadata.chunk_id}\n"
                    f"{hit.chunk.text}"
                )

        if not pdf_provenance_verified:
            raise RuntimeError("retrieval round-trip did not include PDF page provenance")
        if not printed_provenance_verified:
            raise RuntimeError(
                "retrieved results did not include any printed-page reference to verify"
            )
        print(
            "\nPASS: retrieved source metadata and PDF/printed page references match the "
            "processed extraction."
        )
        if queries_with_support == 0:
            print(
                "LIMITATION: none of the queried top-five result sets contained all "
                "their simple topic terms; semantic candidates require manual review."
            )
        else:
            print(
                f"Topic terms appeared in the top-five results for "
                f"{queries_with_support}/{len(QUESTIONS)} queries. This is not a "
                "quality guarantee; inspect the passages and the fallback extraction."
            )
        exit_code = 0
    except Exception as error:
        key = settings.qdrant_api_key if settings is not None else None
        print(f"EDMI RAG verification failed: {_safe_error(error, key)}", file=sys.stderr)
    finally:
        if client is not None:
            if collection_was_absent:
                try:
                    names_after = {
                        collection.name
                        for collection in client.get_collections().collections
                    }
                    if collection_name in names_after:
                        if collection_name == PRODUCTION_COLLECTION:
                            raise RuntimeError("refusing to delete the production collection")
                        client.delete_collection(collection_name=collection_name)
                        print(f"Deleted temporary collection {collection_name}.", flush=True)

                    production_existed_after = PRODUCTION_COLLECTION in names_after
                    if production_existed_after != production_existed_before:
                        raise RuntimeError("production collection existence changed")
                    if production_existed_before:
                        production_count_after = client.count(
                            collection_name=PRODUCTION_COLLECTION,
                            exact=True,
                        ).count
                        if production_count_after != production_count_before:
                            raise RuntimeError("production collection point count changed")
                    print("Production collection remains unchanged.", flush=True)
                except Exception as error:
                    key = settings.qdrant_api_key if settings is not None else None
                    print(
                        f"Cleanup/production verification failed: {_safe_error(error, key)}",
                        file=sys.stderr,
                    )
                    exit_code = 1
            client.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
