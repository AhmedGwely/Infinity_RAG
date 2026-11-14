# Architecture

```
Browser ──▶ web (nginx: React SPA, /api proxy) ──▶ api (FastAPI) ──┬─▶ PostgreSQL  users, sessions, sources, documents,
                                                                   │              chunks (+FTS), jobs, chats, reports, audit
                                                   worker ─────────┤─▶ Redis       rate limits, worker heartbeat
                  (jobs table = durable queue)                     ├─▶ embedder    BGE-M3 on CPU (HTTP, loaded once)
                                                                   └─▶ Qdrant      YOUR existing container, collection rd_engineering
```

## Data model decisions
- **Qdrant stays the vector store** and keeps its payload shape (`text`, nested `metadata`, `tables`). New points add a
  `locator` and `platform` key but remain readable by the legacy API. The platform never calls delete/recreate
  collection (enforced by a unit test); point deletion is limited to explicit ids of platform-owned (non-legacy) chunks.
- **PostgreSQL mirrors chunk text** (point id = chunk id). It is the authority for visibility, enabled/disabled sources,
  citations and keyword search (generated `tsvector`, `simple` configuration so part numbers and Arabic tokens survive).
- **Legacy adoption**: the worker scrolls the collection payloads (no vectors) and inserts `documents`/`chunks` rows
  with `ON CONFLICT DO NOTHING`. Mirrored documents are flagged `legacy` and can never be re-indexed or deleted by the app.

## Retrieval pipeline
1. Semantic: BGE-M3 query vector → Qdrant top-K (over-fetched ×3, then filtered by PostgreSQL visibility).
2. Keyword: `websearch_to_tsquery` full-text rank over visible, ready chunks.
3. Exact: ILIKE on code-like tokens (part numbers, OBIS codes, standard numbers) the tokenizer would split.
4. Reciprocal-rank fusion (weights 1 / 1 / 0.6) → top 8 passages. If the embedder/Qdrant is down, channels 2–3 still work.

## Answer generation and trust
- Provider interface reused from the original project (Gemini / OpenAI / Ollama); all three now separate trusted
  instructions from untrusted evidence. Evidence and question are JSON-encoded inside delimited envelopes.
- The model must structure answers as *Verified from sources* / *Inference* / *Conflicts or gaps* with `[S#]` markers.
- The original strict validator rejects answers with missing, malformed or unknown citation markers. No evidence → the
  model is not called. Citations returned to the UI contain only sources actually cited.
- **Comparison**: the model returns JSON; the server downgrades any "verified" cell that lacks a real citation to
  "not found". This guarantees traceability, not semantic correctness — users open the citation to confirm.

## Ingestion
`upload / sync` → durable job (PostgreSQL, `FOR UPDATE SKIP LOCKED`) → worker parses (PDF via the original PyMuPDF
loader/normaliser/chunker; DOCX, XLSX, PPTX, CSV, text, HTML, JSON/XML natively) → embed in batches → upsert to Qdrant →
replace PostgreSQL chunks → delete superseded points. Failures roll back and compensate; errors are shown per document and per job.
Jobs interrupted by a reboot are re-queued; transient dependency errors retry up to 3 times.
Website sync: robots.txt, scope/regex rules, politeness delay, conditional GET (ETag / Last-Modified), content-hash change
detection, linked-document discovery, per-source schedules.

## Extension points
New connector = a function with the signature `sync_x(session, source, ..., indexer)` plus a branch in `worker._run_sync`
and a form in `pages_admin.jsx`. New file format = a branch in `parsers.parse_file` returning `ParsedDocument`.
New LLM provider = a class implementing `LLMProvider` in `app/generation/providers.py`.
