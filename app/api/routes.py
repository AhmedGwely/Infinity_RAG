"""FastAPI application factory and grounded query endpoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.generation.answer_generator import AnswerGenerator
    from app.retrieval.retriever import Retriever

logger = logging.getLogger(__name__)


def create_app(
    *,
    settings: Settings | None = None,
    retriever: Retriever | None = None,
    answer_generator: AnswerGenerator | None = None,
):
    """Create the API and initialize reusable query components during startup."""
    try:
        from fastapi import FastAPI, HTTPException
        from pydantic import BaseModel, Field
    except ImportError as error:
        raise RuntimeError("Install project dependencies before starting the API.") from error

    from app.core.config import load_settings
    from app.core.schemas import PageReference
    from app.generation.answer_generator import NOT_FOUND_ANSWER
    from app.generation.prompt_templates import validated_citation_ids

    class QueryRequest(BaseModel):
        question: str = Field(min_length=1)

    class SourcePage(BaseModel):
        pdf_page: int
        printed_page: int | None

    class SourceReference(BaseModel):
        citation_id: str
        document_name: str
        document_id: str
        passage: str
        pdf_pages: list[int]
        printed_pages: list[int]
        source_pages: list[SourcePage]
        section_number: str | None
        section_title: str | None
        chunk_id: str

    class QueryResponse(BaseModel):
        answer: str
        sources: list[SourceReference]

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        runtime_settings = settings or load_settings()
        active_retriever = retriever
        active_generator = answer_generator
        if active_retriever is None:
            from app.embeddings.embedding_service import BGEEmbeddingService
            from app.retrieval.retriever import Retriever as RetrieverImplementation
            from app.vectorstore.qdrant_store import QdrantStore

            embeddings = BGEEmbeddingService(settings=runtime_settings)
            store = QdrantStore(settings=runtime_settings)
            active_retriever = RetrieverImplementation(embeddings, store)
        if active_generator is None:
            from app.core.exceptions import ConfigurationError
            from app.generation.answer_generator import AnswerGenerator as GeneratorImplementation
            from app.generation.providers import create_llm_provider

            if runtime_settings.llm_provider.strip().lower() != "gemini":
                raise ConfigurationError(
                    "The /query endpoint requires LLM_PROVIDER=gemini."
                )
            provider = create_llm_provider(runtime_settings)
            active_generator = GeneratorImplementation(provider)
        application.state.retriever = active_retriever
        application.state.answer_generator = active_generator
        yield

    application = FastAPI(
        title="Infinity Egypt R&D Knowledge Assistant",
        version="0.1.0",
        description="Local development API scaffold for engineering document search.",
        lifespan=lifespan,
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        """Report that the API process is responsive."""
        return {"status": "ok"}

    def query(payload):
        """Retrieve indexed evidence and generate a validated, cited answer."""
        if not payload.question.strip():
            raise HTTPException(status_code=422, detail="Question must not be empty.")

        try:
            hits = application.state.retriever.retrieve(payload.question)
        except Exception as error:
            logger.error("Query retrieval failed (%s).", type(error).__name__)
            raise HTTPException(
                status_code=503,
                detail="Document retrieval is temporarily unavailable.",
            ) from None

        try:
            answer = application.state.answer_generator.answer(payload.question, hits)
        except Exception as error:
            logger.error("Query answer generation failed (%s).", type(error).__name__)
            raise HTTPException(
                status_code=502,
                detail="Answer generation failed or returned invalid citations.",
            ) from None

        source_references: list[SourceReference] = []
        if answer == NOT_FOUND_ANSWER:
            cited_ids = []
        else:
            try:
                cited_ids = validated_citation_ids(answer, len(hits))
            except Exception:
                logger.error("Query answer citation validation failed.")
                raise HTTPException(
                    status_code=502,
                    detail="Answer generation failed or returned invalid citations.",
                ) from None
        for source_id in cited_ids:
            metadata = hits[source_id - 1].chunk.metadata
            references = [
                reference
                for reference in metadata.source_pages
                if isinstance(reference, PageReference)
                and isinstance(reference.pdf_page, int)
                and reference.pdf_page > 0
            ]
            if not references and isinstance(metadata.page_number, int) and metadata.page_number > 0:
                references = [PageReference(pdf_page=metadata.page_number)]
            pdf_pages = list(dict.fromkeys(reference.pdf_page for reference in references))
            printed_pages = list(
                dict.fromkeys(
                    reference.printed_page
                    for reference in references
                    if isinstance(reference.printed_page, int) and reference.printed_page > 0
                )
            )
            source_references.append(
                SourceReference(
                    citation_id=f"S{source_id}",
                    document_name=metadata.document_name.strip()
                    or Path(metadata.source_file).name,
                    document_id=metadata.document_id,
                    passage=hits[source_id - 1].chunk.text,
                    pdf_pages=pdf_pages,
                    printed_pages=printed_pages,
                    source_pages=[
                        SourcePage(
                            pdf_page=reference.pdf_page,
                            printed_page=reference.printed_page,
                        )
                        for reference in references
                    ],
                    section_number=metadata.section_number,
                    section_title=metadata.section_title,
                    chunk_id=metadata.chunk_id,
                )
            )

        return QueryResponse(answer=answer, sources=source_references)

    query.__annotations__["payload"] = QueryRequest
    query.__annotations__["return"] = QueryResponse
    application.post("/query", response_model=QueryResponse)(query)
    return application
