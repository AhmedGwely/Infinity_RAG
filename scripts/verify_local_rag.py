"""Verify local BGE-M3 embeddings, Qdrant storage, and semantic retrieval."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qdrant_client import QdrantClient

from app.core.config import load_settings
from app.core.schemas import ChunkMetadata, DocumentChunk, PageReference
from app.embeddings.embedding_service import BGEEmbeddingService, EMBEDDING_DIMENSION
from app.retrieval.retriever import Retriever
from app.vectorstore.qdrant_store import QdrantStore


PRODUCTION_COLLECTION = "rd_engineering"


def _passages() -> list[DocumentChunk]:
    """Create distinct, realistic engineering passages with source provenance."""
    examples = [
        (
            "modbus_rtu",
            "Modbus RTU communication",
            18,
            12,
            "For Modbus RTU, configure every device on the serial bus with a unique "
            "slave address and matching baud rate, parity, and stop bits. The master "
            "frames each request with a CRC-16 checksum and observes the required "
            "silent interval between frames.",
        ),
        (
            "rs485",
            "RS485 network wiring",
            34,
            28,
            "An RS485 trunk should use a daisy-chain topology with a twisted pair for "
            "the differential A and B conductors. Terminate the two physical ends of "
            "the bus, avoid star wiring, and fit bias resistors at one location so "
            "the idle bus has a defined state.",
        ),
        (
            "ppp_modem",
            "PPP modem connectivity",
            57,
            49,
            "A cellular modem can establish remote meter connectivity by dialing the "
            "configured APN and opening a PPP session. The modem negotiates link "
            "control parameters, authenticates when required, and obtains an IP "
            "address before the data concentrator polls the meter.",
        ),
        (
            "cascade_summation",
            "Cascade summation",
            83,
            76,
            "Cascade summation combines energy registers from meters connected in a "
            "hierarchical chain. Configure the parent meter with the downstream "
            "meter addresses and selected import or export registers; the parent "
            "then adds each subordinate reading to its cumulative total while "
            "avoiding duplicate contributions in the cascade.",
        ),
        (
            "meter_security",
            "Meter security controls",
            112,
            103,
            "Protect meter configuration by enabling role-based access, changing "
            "factory credentials, and restricting write commands to authorized "
            "maintenance sessions. Record security events and firmware updates in "
            "the audit log, and disable unused communication services.",
        ),
    ]
    now = datetime.now(timezone.utc)
    chunks: list[DocumentChunk] = []
    for topic, title, pdf_page, printed_page, text in examples:
        chunks.append(
            DocumentChunk(
                text=text,
                metadata=ChunkMetadata(
                    document_id=f"integration-{topic}",
                    document_name=title,
                    document_type="engineering-manual",
                    page_number=pdf_page,
                    section_title=title,
                    chunk_id=str(uuid4()),
                    source_file=f"{topic}.pdf",
                    ingestion_date=now,
                    document_version="integration-test",
                    section_number="1.1",
                    source_pages=(
                        PageReference(pdf_page=pdf_page, printed_page=printed_page),
                    ),
                ),
            )
        )
    return chunks


def main() -> int:
    """Run the live local integration check and clean up its temporary collection."""
    collection_name = f"local_rag_verify_{uuid4().hex}"
    if collection_name == PRODUCTION_COLLECTION:
        print("SAFETY FAILURE: refusing to use the production collection.")
        return 1

    client: QdrantClient | None = None
    collection_is_safe_to_clean = False
    exit_code = 1
    try:
        print("Connecting to Qdrant at http://localhost:6333 ...", flush=True)
        settings = replace(
            load_settings(),
            qdrant_url="http://localhost:6333",
            collection_name=collection_name,
        )
        client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
        )
        existing_names = {
            collection.name for collection in client.get_collections().collections
        }
        if collection_name in existing_names:
            raise RuntimeError("generated temporary collection name already exists")
        collection_is_safe_to_clean = True
        print("Qdrant connection confirmed.", flush=True)

        print(f"Creating temporary collection {collection_name} ...", flush=True)
        store = QdrantStore(settings, client=client)
        collection = client.get_collection(collection_name)
        vector_config = collection.config.params.vectors
        if (
            vector_config.size != EMBEDDING_DIMENSION
            or vector_config.distance.name != "COSINE"
        ):
            raise RuntimeError("temporary collection has unexpected vector configuration")
        print("Temporary 1024-dimensional cosine collection ready.", flush=True)

        print("Loading BAAI/bge-m3 on CPU and embedding five passages ...", flush=True)
        chunks = _passages()
        embeddings = BGEEmbeddingService(
            model_name="BAAI/bge-m3",
            device="cpu",
            batch_size=4,
        )
        try:
            vectors = embeddings.embed_documents([chunk.text for chunk in chunks])
        except Exception as error:
            if embeddings._model is None:
                raise RuntimeError(f"model-loading failure: {error}") from error
            raise RuntimeError(f"embedding failure: {error}") from error
        if len(vectors) != len(chunks):
            raise RuntimeError("embedding failure: embedding count did not match passages")
        print(f"Real model produced {len(vectors)} vectors.", flush=True)

        print("Storing vectors and provenance in Qdrant ...", flush=True)
        store.upsert(chunks, vectors)
        print("Qdrant upsert completed.", flush=True)

        question = (
            "How does a parent meter calculate its cumulative energy total from "
            "downstream meters in cascade summation?"
        )
        print(f"Retrieving for: {question}", flush=True)
        retriever = Retriever(embeddings, store)
        try:
            hits = retriever.retrieve(question, limit=5)
        except Exception as error:
            raise RuntimeError(f"retrieval failure: {error}") from error
        if not hits:
            raise RuntimeError("retrieval failure: Qdrant returned no passages")

        print("\nRetrieved passages:")
        for index, hit in enumerate(hits, start=1):
            metadata = hit.chunk.metadata
            pages = ", ".join(
                f"PDF {page.pdf_page}"
                + (
                    f" (printed {page.printed_page})"
                    if page.printed_page is not None
                    else " (printed page unavailable)"
                )
                for page in metadata.source_pages
            )
            print(
                f"\n{index}. score={hit.score:.4f} | document={metadata.document_name}"
                f" | pages={pages}\n{hit.chunk.text}"
            )

        expected = next(
            chunk for chunk in chunks
            if chunk.metadata.document_id == "integration-cascade_summation"
        )
        matched = next(
            (hit for hit in hits if hit.chunk.metadata.chunk_id == expected.metadata.chunk_id),
            None,
        )
        if matched is None:
            raise RuntimeError("verification failure: expected cascade passage was not returned")
        if matched.chunk != expected:
            raise RuntimeError(
                "verification failure: retrieved passage metadata or page provenance changed"
            )
        print(
            "\nPASS: expected cascade-summation passage retrieved with its original "
            "metadata and PDF/printed-page provenance."
        )
        exit_code = 0
    except Exception as error:
        print(f"INTEGRATION FAILURE: {error}", file=sys.stderr, flush=True)
    finally:
        if client is not None and collection_is_safe_to_clean:
            try:
                names = {
                    collection.name for collection in client.get_collections().collections
                }
                if collection_name in names:
                    if collection_name == PRODUCTION_COLLECTION:
                        raise RuntimeError("refusing to delete the production collection")
                    client.delete_collection(collection_name=collection_name)
                    print(f"Deleted temporary collection {collection_name}.", flush=True)
            except Exception as error:
                print(
                    f"CLEANUP FAILURE for temporary collection {collection_name}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                exit_code = 1
            finally:
                client.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
