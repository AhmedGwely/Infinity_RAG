# Infinity Egypt R&D Knowledge Assistant

A modular Python 3.12 engineering-document RAG project. The PDF ingestion milestone extracts page-aware text/tables, creates section-aware chunks, and writes JSON plus a quality report. Embeddings, Qdrant indexing, retrieval, and generated answers are still later milestones.

## Architecture

- `app/core`: central environment settings, logging, shared errors, and typed document/chunk/search schemas. Every chunk schema carries document ID/name/type, source file, page, section, chunk ID, and ingestion timestamp.
- `app/ingestion`: PyMuPDF validation/page extraction/table fallback, Docling structured parsing, conservative normalization, section-aware chunking, JSON output, and quality reports.
- `app/embeddings`: provider contract and planned local BGE-M3 adapter. The model is not downloaded by setup.
- `app/vectorstore`: vector store contract and planned Qdrant adapter, including payload provenance.
- `app/retrieval`: semantic retrieval, dense/keyword fusion, and an optional reranker interface.
- `app/generation`: provider-independent LLM contract, citation-first prompt, and answer orchestration. An empty evidence set returns an explicit not-found answer.
- `app/api`: FastAPI application factory with a working health endpoint and a query endpoint that intentionally returns HTTP 501 until the RAG pipeline is implemented.
- `frontend`: Streamlit scaffold only; upload and chat workflows are deferred.
- `documents`: incoming, processed, and archive locations. Document contents are ignored by Git.
- `storage`: reserved for local vector/database artifacts; generated data is ignored by Git.
- `tests`: generated-PDF ingestion/chunk tests and a conditional EDMI manual integration test. Embedding and retrieval tests remain deferred.

Interfaces (`EmbeddingService`, `VectorStore`, and `LLMProvider`) keep model and storage choices replaceable. The default LLM configuration points to local Ollama; OpenAI is an optional future adapter. No company documents are sent to any external service by this scaffold.

## Ingestion Status

Implemented: settings loading, page-level PyMuPDF extraction, document metadata and filename version parsing, explicit printed-page detection, table structures and Markdown, Docling parsing with an offline fallback, conservative normalization, section-aware chunking, JSON output, and extraction quality reports.

Still deferred: BGE-M3 inference, Qdrant persistence, hybrid retrieval, reranking, concrete LLM adapters, and end-to-end UI flows. Printed page numbers are populated only when an explicit `Page N`-style footer can be identified; unlabeled footer numbers remain null. Repeated headers/footers are retained because this pipeline does not yet have sufficient geometric confidence to remove them. Docling is offline by default; if its models are not already cached or structured parsing fails, the pipeline records the reason and uses PyMuPDF text/table extraction, marking affected pages for review.

## Dependencies

Declared in `requirements.txt`:

- FastAPI and Uvicorn for the API.
- Streamlit for the UI.
- Qdrant Client for vector storage.
- Docling and PyMuPDF for structured PDF parsing and page-aware PDF operations.
- Sentence Transformers for a later local BGE-M3-compatible embedding adapter.
- OpenAI SDK for an optional provider adapter.
- python-dotenv for `.env` loading.
- pytest for tests.

Dependency installation is opt-in in the setup script. Docling can be large; no package or model is installed or downloaded automatically by the ingestion command.

## Windows Setup

From the project root in PowerShell:

```powershell
.\scripts\setup_environment.ps1
.\RAG_env\Scripts\Activate.ps1
Copy-Item .env.example .env
```

Edit `.env` for local configuration. To explicitly install dependencies after reviewing the package list:

```powershell
.\scripts\setup_environment.ps1 -InstallDependencies
```

The ingestion pipeline requires the already-declared `PyMuPDF` and `docling` dependencies. `DOCLING_OFFLINE=true` prevents automatic model downloads; Docling will use cached assets or fall back visibly to PyMuPDF.

## Run PDF Ingestion

Process the EDMI manual into `documents/processed/`:

```powershell
python -m app.ingestion.ingestion_pipeline --input "D:\Manuals\Mk6Es-Mk6ESL User Manual v1.3.pdf"
```

Process every PDF in `documents/incoming/`:

```powershell
python -m app.ingestion.ingestion_pipeline
```

Override the source or output directory:

```powershell
python -m app.ingestion.ingestion_pipeline --input "D:\Manuals" --output "documents/processed"
```

Each processed PDF produces `<document-id>.json` with page records, structured tables, chunks, and an embedded report, plus `<document-id>.quality.json`. Page numbers are physical PDF pages (1-indexed); printed pages are separate and may be null. Original PDFs are read-only inputs.

Run ingestion tests:

```powershell
pytest tests\test_pdf_ingestion.py tests\test_chunker.py
```

The EDMI integration test runs only when the file is at `D:\Manuals\Mk6Es-Mk6ESL User Manual v1.3.pdf` and both PyMuPDF and Docling are installed; otherwise pytest reports it as skipped.

## Run Locally

In one activated terminal, start the API:

```powershell
.\scripts\run_app.ps1
```

The API health check is at `http://127.0.0.1:8000/health`; interactive API docs are at `http://127.0.0.1:8000/docs`. The query route returns 501 until retrieval and generation are implemented.

In a second activated terminal, start the current UI scaffold:

```powershell
streamlit run frontend/streamlit_app.py
```

The Streamlit page currently reports that ingestion and chat are not implemented.

The original root `app.py` is retained unchanged as a legacy starter. `main.py` is the API entry point.

## Milestones

1. Validate extraction quality against one EDMI manual run and inspect flagged pages/tables.
2. Generate local embeddings and persist chunks to Qdrant.
3. Add semantic plus keyword retrieval and evaluate recall on engineering questions.
4. Add a local LLM adapter, grounded answers, and citations; add cloud providers only after data-handling approval.
5. Connect the Streamlit upload and question workflows to the API.
