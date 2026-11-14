# INFINITY AI — Intelligent Engineering & Knowledge Platform

A self-hosted, browser-based knowledge platform for engineering teams: ask technical questions, search and compare
documents, ingest manuals / websites / APIs, and generate reports — where **every answer links to the source passage
it came from**, and verified evidence is kept separate from inference.

It builds on the original Infinity_RAG project (BGE-M3 + Qdrant + Gemini + strict citation validation), which is
preserved: the existing `app/` package is reused and the existing Qdrant collection is used **as-is** (never
deleted, recreated, overwritten or re-indexed). The Streamlit UI is replaced by a React web application.

## Quick start (Windows 11, Docker Desktop)

```powershell
.\start.ps1
```

That single command inspects your existing `infinity-qdrant` container (read-only), prepares `.env` (appending only
what is missing), builds and starts everything, waits until healthy, prints the first-run admin login, and opens
**http://localhost:8088**. The first start downloads the BGE-M3 model (~2.3 GB, cached in a Docker volume).

| Command | Purpose |
|---|---|
| `.\start.ps1` | Build/start/update the platform |
| `.\stop.ps1` | Stop containers (all data kept) |
| `.\backup.ps1` | Back up PostgreSQL, uploads and a Qdrant snapshot |
| `python scripts/verify_platform.py` | Live end-to-end verification against the running system |

Documentation: [Operations & deployment](docs/OPERATIONS.md) · [Architecture](docs/ARCHITECTURE.md) ·
[Verification report](docs/VERIFICATION.md) · [Original README](docs/LEGACY_README.md)

## What is in the box

- **Assistant** — grounded Q&A in English/Arabic with clickable citations (document, page/slide/sheet/URL, passage, index time).
- **Research** — hybrid retrieval (semantic + keyword + exact part-number/OBIS match, rank-fused) with no LLM involved.
- **Library** — upload PDF, DOCX, XLSX, PPTX, CSV, TXT, MD, JSON, XML, HTML; background indexing with live status.
- **Sources** — website crawler (robots.txt, scope rules, document discovery, change detection, schedules) and REST JSON/XML connector.
- **Compare / Reports** — evidence-checked specification tables and cited reports, exportable to Markdown/CSV.
- **Administration** — users and roles, per-source/document visibility, encrypted provider credentials, audit log, health.

## Services (Docker Compose)

`web` (nginx + React, the only published port) → `api` (FastAPI) · `worker` (indexing, sync, scheduling) ·
`embedder` (BGE-M3, CPU, loaded once) · `postgres` · `redis`. Qdrant is **your existing container**, reached over the
host port; an optional bundled Qdrant exists behind a Compose profile for fresh machines only.

## Honest scope notes

See [docs/VERIFICATION.md](docs/VERIFICATION.md) for exactly what is implemented, what was tested, and what still needs
external access or a first run on your machine (e.g. no cross-encoder reranker, no OCR, no database connectors yet).
