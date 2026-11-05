# Infinity Egypt R&D Knowledge Assistant

A modular Python 3.12 engineering-document RAG project. PDF ingestion extracts page-aware text/tables, creates section-aware chunks, and writes JSON plus a quality report. Processed chunks are embedded and indexed in Qdrant; the FastAPI service provides grounded, cited answers, and the Streamlit chat interface connects to that API.

## Architecture

- `app/core`: central environment settings, logging, shared errors, and typed document/chunk/search schemas. Every chunk schema carries document ID/name/type, source file, page, section, chunk ID, and ingestion timestamp.
- `app/ingestion`: PyMuPDF validation/page extraction/table fallback, Docling structured parsing, conservative normalization, section-aware chunking, JSON output, and quality reports.
- `app/embeddings`: provider contract and local normalized BGE-M3 adapter. The model is not downloaded by setup.
- `app/vectorstore`: vector store contract and Qdrant adapter, including payload provenance.
- `app/retrieval`: semantic retrieval through the shared embedding and vector store interfaces.
- `app/generation`: provider-independent LLM contract, citation-first prompt, and answer orchestration. An empty evidence set returns an explicit not-found answer.
- `app/api`: FastAPI application factory with health and grounded query endpoints, including citations and their source passages.
- `frontend`: Streamlit chat interface that sends questions to the FastAPI query endpoint and displays answer citations and source passages.
- `documents`: incoming, processed, and archive locations. Document contents are ignored by Git.
- `storage`: reserved for local vector/database artifacts; generated data is ignored by Git.
- `tests`: ingestion, embedding, retrieval, API, vector-store, and Streamlit interface tests, including a conditional EDMI manual integration test.

Interfaces (`EmbeddingService`, `VectorStore`, and `LLMProvider`) keep model and storage choices replaceable. The query API requires `LLM_PROVIDER=gemini`; when enabled, the configured provider receives the question and retrieved evidence. The Streamlit frontend communicates only with the FastAPI service.

## Ingestion Status

Implemented: settings loading, page-level PyMuPDF extraction, document metadata and filename version parsing, explicit printed-page detection, table structures and Markdown, Docling parsing with an offline fallback, conservative normalization, section-aware chunking, JSON output, and extraction quality reports.

Printed page numbers are populated only when an explicit `Page N`-style footer can be identified; unlabeled footer numbers remain null. Repeated headers/footers are retained because this pipeline does not yet have sufficient geometric confidence to remove them. Docling is offline by default; if its models are not already cached or structured parsing fails, the pipeline records the reason and uses PyMuPDF text/table extraction, marking affected pages for review.

## Dependencies

Declared in `requirements.txt`:

- FastAPI and Uvicorn for the API.
- Streamlit for the UI.
- Qdrant Client for vector storage.
- Docling and PyMuPDF for structured PDF parsing and page-aware PDF operations.
- Sentence Transformers for the local BGE-M3-compatible embedding adapter.
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

The API health check is at `http://127.0.0.1:8000/health`; interactive API docs are at `http://127.0.0.1:8000/docs`. The `/query` endpoint accepts `{"question": "..."}` and returns a grounded `answer` plus `sources`, including passage text and physical/printed page citations. Answer generation requires `LLM_PROVIDER=gemini` and a configured provider key.

In a second activated terminal, start the chat interface:

```powershell
streamlit run frontend/streamlit_app.py
```

The UI connects to `http://<API_HOST>:<API_PORT>` by default. Set `API_URL` in `.env`
when the API is hosted at a different address. The API must be configured with
`LLM_PROVIDER=gemini` and a valid key to generate answers; the Streamlit interface
does not call Gemini directly.

The API currently does not expose a document catalog, so the sidebar reports that
document availability is not exposed rather than inventing collection statistics.

The original root `app.py` is retained unchanged as a legacy starter. `main.py` is the API entry point.

## Current Scope

The Streamlit app provides session-scoped chat history, backend health status, answer formatting, and expandable citations with source passages. `API_URL` optionally overrides the API address; otherwise it is built from `API_HOST` and `API_PORT`. A document catalog is not currently exposed by the API, so the UI does not report document counts or invent availability information.
