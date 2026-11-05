"""Read-only retrieval evaluation against the production EDMI Qdrant collection."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qdrant_client import QdrantClient, models

from app.core.config import Settings, load_settings
from app.core.schemas import SearchHit
from app.embeddings.embedding_service import BGEEmbeddingService, EMBEDDING_DIMENSION
from app.retrieval.retriever import Retriever
from app.vectorstore.qdrant_store import QdrantStore


PRODUCTION_COLLECTION = "rd_engineering"


@dataclass(frozen=True)
class EvaluationQuestion:
    """One manual-grounded query or paraphrase used in the evaluation."""

    case_id: str
    question: str
    expected_terms: tuple[str, ...]
    table_applicable: bool = False
    paraphrase_of: str | None = None
    table_probe: bool = False
    expected_table_terms: tuple[str, ...] = ()


QUESTIONS = (
    EvaluationQuestion(
        case_id="power-factor-indication",
        question=(
            "How can I identify whether the meter is operating with leading or lagging "
            "power factor using its indicator lamps?"
        ),
        expected_terms=("power factor", "lead or lag", "unit of q"),
    ),
    EvaluationQuestion(
        case_id="modbus-rtu",
        question=(
            "Which Modbus RTU function code is supported, and what are its access "
            "permissions and communication interfaces?"
        ),
        expected_terms=(
            "modbus rtu",
            "feature code 503",
            "read access only",
            "rs-232",
            "rs-485",
            "ethernet",
        ),
        table_applicable=True,
        expected_table_terms=("modbus", "powerfactor"),
    ),
    EvaluationQuestion(
        case_id="modem-diagnostics",
        question=(
            "How does the meter monitor modem communication status, and which "
            "registers provide diagnostic information?"
        ),
        expected_terms=(
            "modem mode",
            "last occurred error",
            "network info",
            "ppp connection established",
        ),
    ),
    EvaluationQuestion(
        case_id="voltage-configurations",
        question=(
            "What voltage configurations are required for medium-voltage metering "
            "applications?"
        ),
        expected_terms=(
            "2-vt, 2-ct",
            "3-vt, 3-ct",
            "3-phase 3-wire",
            "3-phase 4-wire",
        ),
    ),
    EvaluationQuestion(
        case_id="dnp3-support",
        question=(
            "How does the meter support DNP3 communication, and what feature code is "
            "associated with it?"
        ),
        expected_terms=(
            "dnp3",
            "feature code 27",
            "read out",
            "rs-232",
            "rs-485",
        ),
    ),
    EvaluationQuestion(
        case_id="power-factor-indication",
        question=(
            "How does the power-factor display show whether reactive power is leading "
            "or lagging?"
        ),
        expected_terms=("power factor", "lead or lag", "unit of q"),
        paraphrase_of="power-factor-indication",
    ),
    EvaluationQuestion(
        case_id="modem-diagnostics",
        question=(
            "Which meter values can help determine whether the modem has connected "
            "and diagnose a communication failure?"
        ),
        expected_terms=(
            "modem mode",
            "last occurred error",
            "network info",
            "ppp connection established",
        ),
        paraphrase_of="modem-diagnostics",
    ),
    EvaluationQuestion(
        case_id="modbus-default-mapping",
        question="Which measurements appear in the meter's default Modbus register mapping?",
        expected_terms=("phaseavoltage", "powerfactor", "modbus"),
        table_applicable=True,
        table_probe=True,
        expected_table_terms=("phaseavoltage", "powerfactor"),
    ),
)


class EmbeddingQuery(Protocol):
    """Minimal query embedding interface accepted by the production Retriever."""

    def embed_query(self, text: str) -> list[float]:
        ...


@dataclass(frozen=True)
class QuestionResult:
    """Retrieved hits and heuristic metrics for one evaluation query."""

    test: EvaluationQuestion
    hits: tuple[SearchHit, ...]
    matched_terms: tuple[str, ...]
    matched_table_terms: tuple[str, ...]
    relevant_result_found: bool
    source_page_metadata_exists: bool
    physical_page_provenance_valid: bool
    table_retrieved: bool
    shared_result_count: int | None = None
    error: str | None = None

    @property
    def keyword_coverage(self) -> float:
        if not self.test.expected_terms:
            return 0.0
        return len(self.matched_terms) / len(self.test.expected_terms)

    @property
    def table_support_found(self) -> bool:
        return (
            self.test.table_applicable
            and bool(self.test.expected_table_terms)
            and all(term in self.matched_table_terms for term in self.test.expected_table_terms)
        )


@dataclass(frozen=True)
class EvaluationReport:
    """Run metadata, question outcomes, and read-only safety verification."""

    evaluated_at: datetime
    collection_name: str
    embedding_model: str
    top_k: int
    initial_point_count: int
    final_point_count: int
    results: tuple[QuestionResult, ...]

    @property
    def point_count_unchanged(self) -> bool:
        return self.initial_point_count == self.final_point_count


def _hit_content(hit: SearchHit) -> str:
    """Combine stored text and extracted table strings for coverage checks."""
    parts = [hit.chunk.text]
    for table in hit.chunk.tables:
        parts.extend(
            value
            for value in (
                table.caption,
                table.markdown,
                *table.column_headers,
                *(cell for row in table.rows for cell in row),
            )
            if value
        )
    return "\n".join(parts).casefold()


def _hit_table_content(hit: SearchHit) -> str:
    parts: list[str] = []
    for table in hit.chunk.tables:
        parts.extend(
            value
            for value in (
                table.caption,
                table.markdown,
                *table.column_headers,
                *(cell for row in table.rows for cell in row),
            )
            if value
        )
    return "\n".join(parts).casefold()


def _score_result(
    test: EvaluationQuestion,
    hits: list[SearchHit],
) -> QuestionResult:
    contents = [_hit_content(hit) for hit in hits]
    matched_terms = tuple(
        term
        for term in test.expected_terms
        if any(term.casefold() in content for content in contents)
    )
    table_contents = [_hit_table_content(hit) for hit in hits]
    matched_table_terms = tuple(
        term
        for term in test.expected_table_terms
        if any(term.casefold() in content for content in table_contents)
    )
    page_metadata_exists = bool(hits) and any(
        bool(hit.chunk.metadata.source_pages) for hit in hits
    )
    physical_pages_valid = bool(hits) and all(
        hit.chunk.metadata.source_pages
        and all(
            isinstance(page.pdf_page, int)
            and not isinstance(page.pdf_page, bool)
            and page.pdf_page > 0
            for page in hit.chunk.metadata.source_pages
        )
        for hit in hits
    )
    return QuestionResult(
        test=test,
        hits=tuple(hits),
        matched_terms=matched_terms,
        matched_table_terms=matched_table_terms,
        relevant_result_found=bool(matched_terms),
        source_page_metadata_exists=page_metadata_exists,
        physical_page_provenance_valid=physical_pages_valid,
        table_retrieved=any(hit.chunk.tables for hit in hits),
    )


def _validate_top_k(top_k: int) -> None:
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError("top_k must be a positive integer")


def _validate_collection_configuration(collection: Any) -> None:
    vector_config = collection.config.params.vectors
    if not isinstance(vector_config, models.VectorParams):
        raise RuntimeError("production collection must use one dense vector")
    if (
        vector_config.size != EMBEDDING_DIMENSION
        or vector_config.distance != models.Distance.COSINE
    ):
        raise RuntimeError(
            f"production collection must use {EMBEDDING_DIMENSION}-dimensional "
            "cosine vectors"
        )


def evaluate_retrieval(
    *,
    settings: Settings | None = None,
    client: QdrantClient | None = None,
    embeddings: EmbeddingQuery | None = None,
    top_k: int = 5,
) -> EvaluationReport:
    """Evaluate the production Retriever and verify Qdrant point count is unchanged."""
    _validate_top_k(top_k)
    settings = settings or load_settings()
    if settings.collection_name != PRODUCTION_COLLECTION:
        raise RuntimeError(
            f"refusing evaluation: collection must be {PRODUCTION_COLLECTION!r}"
        )

    owns_client = client is None
    qdrant = (
        client
        if client is not None
        else QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key,
        )
    )
    try:
        collection_names = {
            item.name for item in qdrant.get_collections().collections
        }
        if PRODUCTION_COLLECTION not in collection_names:
            raise RuntimeError(
                f"production collection {PRODUCTION_COLLECTION!r} does not exist; "
                "read-only evaluation will not create it"
            )

        initial_count = qdrant.count(
            collection_name=PRODUCTION_COLLECTION,
            exact=True,
        ).count
        _validate_collection_configuration(qdrant.get_collection(PRODUCTION_COLLECTION))

        store = QdrantStore(
            settings,
            client=qdrant,
            create_if_missing=False,
        )
        embedding_service = embeddings or BGEEmbeddingService(
            model_name=settings.embedding_model,
            device="cpu",
            batch_size=4,
            settings=settings,
        )
        retriever = Retriever(embedding_service, store)

        results: list[QuestionResult] = []
        original_hit_ids: dict[str, set[str]] = {}
        for test in QUESTIONS:
            try:
                hits = retriever.retrieve(test.question, limit=top_k)
                result = _score_result(test, hits)
                if test.paraphrase_of is None:
                    original_hit_ids[test.case_id] = {
                        hit.chunk.metadata.chunk_id for hit in hits
                    }
            except Exception as error:
                result = QuestionResult(
                    test=test,
                    hits=(),
                    matched_terms=(),
                    matched_table_terms=(),
                    relevant_result_found=False,
                    source_page_metadata_exists=False,
                    physical_page_provenance_valid=False,
                    table_retrieved=False,
                    error=_safe_error(error, settings),
                )
            results.append(result)

        for index, result in enumerate(results):
            original_id = result.test.paraphrase_of
            if original_id is None:
                continue
            shared = len(
                {
                    hit.chunk.metadata.chunk_id
                    for hit in result.hits
                }
                & original_hit_ids.get(original_id, set())
            )
            results[index] = QuestionResult(
                test=result.test,
                hits=result.hits,
                matched_terms=result.matched_terms,
                matched_table_terms=result.matched_table_terms,
                relevant_result_found=result.relevant_result_found,
                source_page_metadata_exists=result.source_page_metadata_exists,
                physical_page_provenance_valid=result.physical_page_provenance_valid,
                table_retrieved=result.table_retrieved,
                shared_result_count=shared,
                error=result.error,
            )

        final_count = qdrant.count(
            collection_name=PRODUCTION_COLLECTION,
            exact=True,
        ).count
        return EvaluationReport(
            evaluated_at=datetime.now(timezone.utc),
            collection_name=PRODUCTION_COLLECTION,
            embedding_model=settings.embedding_model,
            top_k=top_k,
            initial_point_count=initial_count,
            final_point_count=final_count,
            results=tuple(results),
        )
    finally:
        if owns_client:
            qdrant.close()


def _safe_error(error: Exception, settings: Settings) -> str:
    message = str(error)
    for secret in (settings.qdrant_api_key, settings.qdrant_url):
        if secret:
            message = message.replace(secret, "[redacted]")
    return f"{type(error).__name__}: {message}"


def _print_result(result: QuestionResult, top_k: int) -> None:
    if result.test.table_probe:
        kind = "table-specific probe"
    elif result.test.paraphrase_of:
        kind = f"paraphrase of {result.test.paraphrase_of}"
    else:
        kind = "original"
    print(f"\n[{result.test.case_id} | {kind}] {result.test.question}")
    if result.error:
        print(f"  FAILURE: {result.error}")
        return

    print(f"  Results returned: {len(result.hits)} (top_k={top_k})")
    print(
        f"  Relevant result found (keyword heuristic): "
        f"{result.relevant_result_found}"
    )
    print(
        f"  Expected-term coverage (heuristic): "
        f"{len(result.matched_terms)}/{len(result.test.expected_terms)} "
        f"({result.keyword_coverage:.0%}); matched: "
        f"{', '.join(result.matched_terms) or 'none'}"
    )
    print(
        f"  Source page metadata exists: {result.source_page_metadata_exists}; "
        f"physical page provenance valid: {result.physical_page_provenance_valid}"
    )
    table_status = (
        "retrieved" if result.table_retrieved else "not retrieved"
    ) if result.test.table_applicable else (
        "retrieved" if result.table_retrieved else "not applicable"
    )
    print(
        f"  Extracted table expected: {result.test.table_applicable}; "
        f"table data: {table_status}"
    )
    if result.test.table_applicable:
        print(
            f"  Expected table-term coverage: "
            f"{len(result.matched_table_terms)}/"
            f"{len(result.test.expected_table_terms)}; "
            f"matched: {', '.join(result.matched_table_terms) or 'none'}; "
            f"table-specific support: {result.table_support_found}"
        )
    if result.shared_result_count is not None:
        print(
            f"  Chunk IDs shared with original query: "
            f"{result.shared_result_count}/{len(result.hits)}"
        )

    for rank, hit in enumerate(result.hits, start=1):
        metadata = hit.chunk.metadata
        physical_pages = ", ".join(
            str(page.pdf_page) for page in metadata.source_pages
        ) or "unavailable"
        printed_pages = ", ".join(
            str(page.printed_page)
            for page in metadata.source_pages
            if page.printed_page is not None
        ) or "unavailable"
        print(
            f"\n  {rank}. score={hit.score:.6f} | document="
            f"{metadata.document_name} | version={metadata.document_version or 'unavailable'}"
        )
        print(
            f"     physical PDF pages: {physical_pages} | printed pages: "
            f"{printed_pages}"
        )
        print(
            f"     section: {metadata.section_number or 'unavailable'} "
            f"{metadata.section_title or ''} | extracted tables: "
            f"{'yes' if hit.chunk.tables else 'no'}"
        )
        print(f"     chunk_id: {metadata.chunk_id}")
        print(f"     text: {hit.chunk.text}")
        for table in hit.chunk.tables:
            print(f"     table {table.table_id}: {table.caption or 'no caption'}")
            print(f"       {table.markdown}")


def _print_report(report: EvaluationReport) -> None:
    originals = [
        result
        for result in report.results
        if result.test.paraphrase_of is None and not result.test.table_probe
    ]
    paraphrases = [result for result in report.results if result.test.paraphrase_of]
    table_probes = [result for result in report.results if result.test.table_probe]
    print("READ-ONLY RETRIEVAL EVALUATION")
    print(f"Evaluation time (UTC): {report.evaluated_at.isoformat()}")
    print(f"Collection: {report.collection_name}")
    print(f"Initial point count: {report.initial_point_count}")
    print(f"Final point count: {report.final_point_count}")
    print(f"Point count unchanged: {report.point_count_unchanged}")
    print(f"Embedding model: {report.embedding_model}")
    print(f"Original test questions: {len(originals)}")
    print(f"Paraphrase queries: {len(paraphrases)}")
    print(f"Supplemental table-specific probes: {len(table_probes)}")
    print(f"Top_k: {report.top_k}")
    print(
        "Keyword matching is a heuristic over retrieved chunk text and attached "
        "table fields, not a formal semantic relevance score."
    )

    for result in report.results:
        _print_result(result, report.top_k)

    successful_originals = [result for result in originals if result.error is None]
    expected_total = sum(
        len(result.test.expected_terms) for result in successful_originals
    )
    matched_total = sum(len(result.matched_terms) for result in successful_originals)
    relevant_count = sum(
        result.relevant_result_found for result in successful_originals
    )
    page_count = sum(
        result.source_page_metadata_exists for result in successful_originals
    )
    table_tests = [
        result
        for result in [*successful_originals, *table_probes]
        if result.test.table_applicable and result.error is None
    ]
    table_count = sum(result.table_support_found for result in table_tests)
    print("\nAGGREGATE (five original questions)")
    print(
        f"Questions with a heuristic relevant hit: {relevant_count}/"
        f"{len(successful_originals)}"
    )
    print(
        f"Expected-term coverage: {matched_total}/{expected_total} "
        f"({matched_total / expected_total:.0%})"
        if expected_total
        else "Expected-term coverage: unavailable"
    )
    print(f"Questions with source page metadata: {page_count}/{len(successful_originals)}")
    print(
        f"Applicable table tests with expected terms in attached table data: "
        f"{table_count}/{len(table_tests)}"
    )
    print(
        f"Total results returned: "
        f"{sum(len(result.hits) for result in successful_originals)}"
    )
    for result in table_probes:
        print(
            f"Table probe '{result.test.case_id}': attached table data="
            f"{result.table_retrieved}, expected table terms matched="
            f"{', '.join(result.matched_table_terms) or 'none'}; "
            f"table-specific support={result.table_support_found}."
        )
    if paraphrases:
        print("\nPARAPHRASE COMPARISON")
        for result in paraphrases:
            original = next(
                (
                    candidate
                    for candidate in originals
                    if candidate.test.case_id == result.test.paraphrase_of
                ),
                None,
            )
            original_coverage = (
                f"{original.keyword_coverage:.0%}" if original is not None else "n/a"
            )
            print(
                f"{result.test.paraphrase_of}: original expected-term coverage="
                f"{original_coverage}, paraphrase coverage="
                f"{result.keyword_coverage:.0%}, shared top-k chunk IDs="
                f"{result.shared_result_count or 0}; inspect retrieved passages above."
            )

    print("\nFAILURES AND LIMITATIONS")
    failures = [
        f"{result.test.case_id}: {result.error}"
        for result in report.results
        if result.error
    ]
    if not report.point_count_unchanged:
        failures.append(
            "Qdrant point count changed during evaluation; the script did not issue "
            "any write operation."
        )
    if failures:
        for failure in failures:
            print(f"- {failure}")
    else:
        print("- No retrieval or point-count failures recorded.")
    if any(not result.table_support_found for result in table_tests):
        print(
            "- At least one table-applicable query returned no extracted table with "
            "its expected table-specific terms."
        )
    print(
        "- Expected terms are grounded in the processed manual. The manual describes "
        "power-factor lead/lag display rather than identifying indicator lamps."
    )
    print(
        "- The Modbus passage specifies read access and extended feature code 503; "
        "it does not identify a Modbus protocol function code by that name."
    )
    print(
        "- The manual's extracted wiring text identifies 2-VT/2-CT and 3-VT/3-CT "
        "configurations, but does not explicitly label them as medium-voltage "
        "requirements."
    )
    print(
        "- No precision, recall, MRR, or nDCG is reported because no human-verified "
        "relevance labels were supplied."
    )


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("top_k must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("top_k must be a positive integer")
    return parsed


def main(argv: list[str] | None = None) -> int:
    """Run retrieval-only evaluation and return nonzero on any recorded failure."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k", type=_positive_int, default=5)
    args = parser.parse_args(argv)
    settings: Settings | None = None
    try:
        settings = load_settings()
        report = evaluate_retrieval(settings=settings, top_k=args.top_k)
    except Exception as error:
        if settings is None:
            message = f"{type(error).__name__}: {error}"
        else:
            message = _safe_error(error, settings)
        print(f"Retrieval evaluation failed: {message}", file=sys.stderr)
        return 1

    _print_report(report)
    has_query_failure = any(result.error for result in report.results)
    return 0 if report.point_count_unchanged and not has_query_failure else 1


if __name__ == "__main__":
    raise SystemExit(main())
