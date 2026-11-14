# Verification report

Status legend — **Tested here**: executed in the build sandbox (no Docker, no network, no PostgreSQL/Qdrant/Redis).
**Needs your run**: implemented and statically checked, but it can only be proven on your machine.
Nothing below is claimed as passing unless it was actually executed.

## Automated tests executed during development
| Suite | Result | Covers |
|---|---|---|
| `backend/tests` (unittest, 42 tests) | **passing** | password hashing & policy, credential encryption, SSRF guard, rank fusion, text splitting, DOCX/XLSX/PPTX/CSV/Arabic-text parsers on real generated files, HTML extraction, website crawler (robots.txt, scope, link discovery, ETag/304, content-hash change detection, document download, error handling), REST/XML connector, citation validation, prompt-injection envelope, comparison "verified-needs-citation" contract, visibility clearance, no-delete-collection invariant, ORM↔migration schema parity |
| `web/test` (esbuild + React server render) | **passing** (19 checks) | frontend bundles; every page renders; Markdown renderer produces citation chips/tables and escapes injected HTML |
| Static analysis | clean | no undefined names; every import of the legacy `app/` package resolves to a real symbol |
| Legacy provider patch | verified with a fake client | OpenAI adapter now supports system-instruction separation |

Run them: `cd backend && PYTHONPATH=.:.. python -m unittest discover -s tests -t .` and `cd web && npm install && node test/run.mjs`.

## Definition-of-done checklist
| Requirement | Implemented | Verified |
|---|---|---|
| Authorised user can log in | yes (cookie sessions, rate-limited) | Needs your run → `verify_platform.py` |
| Admin manages users & permissions | yes (roles, disable, reset, guards for last admin) | Needs your run |
| Engineer uploads & searches documents | yes (9 formats, background indexing) | Parsers tested here; end-to-end needs your run |
| Admin registers a website and configures ingestion | yes (GUI form, test, sync, schedule) | Crawler logic tested here with a fake fetcher; live fetch needs your run |
| APIs connectable via GUI | yes (REST GET, JSON/XML, bearer/header auth) | Parsing tested here; live call needs your run |
| Sources synchronised and indexed | yes (durable jobs, incremental, change detection) | Logic tested; full pipeline needs your run |
| Assistant answers from multiple sources | yes (hybrid retrieval, fused) | Needs your run (+ Gemini key) |
| Citations can be opened and verified | yes (drawer with passage + context + original file/URL) | UI renders; live behaviour needs your run |
| Technical comparisons from evidence | yes (+ CSV/Markdown export) | Contract tested here; live needs your run |
| Errors/ingestion failures visible | yes (per-document error, jobs table, banner, retry) | Needs your run |
| Data persists across restart/reboot | yes (named volumes, `restart: unless-stopped`, job recovery) | Needs your run (reboot test below) |
| Documented deployment | yes (`README`, `OPERATIONS.md`, `start.ps1`) | `start.ps1` could not be executed here (no PowerShell) |
| Existing Qdrant data preserved | yes (never deleted/recreated; read-only mirror; legacy docs protected) | Invariants unit-tested; confirm point count after first run |

## First-run acceptance procedure (≈15 minutes plus model download)
1. `.\start.ps1` — note the point count it prints for `rd_engineering` (expected 631).
2. `python scripts/verify_platform.py --llm` — runs ~30 live checks (auth, CSRF, role isolation, mirror parity,
   upload→index→hybrid search→citation, real Gemini answer with validated citations, deletion leaves existing points intact).
   It writes `docs/verification-results.json`.
3. Reboot test: restart Windows, sign in, wait for Docker Desktop, confirm http://localhost:8088 works, documents are still listed,
   and `verify_platform.py` still passes.
4. In the browser: Library → Mk6ES manual shows "pre-existing index" with 631 passages; ask a question; click a citation.

## Known limitations and items dependent on external access
- **Never executed here**: Docker builds, PostgreSQL migration, FastAPI runtime, worker loop, Qdrant calls, BGE-M3 model, Gemini. Expect
  to fix small integration issues on first run; `docker compose logs api worker` and `verify_platform.py` will point at them.
- **Gemini**: the platform reuses your existing provider code and `.env` values. The default model name in the repo
  (`gemini-3.8-flash`) is passed through unchanged; use Providers → Test connection to confirm the model and key.
- **Reranking**: only rank fusion; no cross-encoder reranker (memory/latency on 16 GB CPU). Interface point: `rag.retrieve`.
- **OCR**: scanned PDFs yield no text and are reported as such. **Docling** is not in the image (PyMuPDF path only).
- **Table/numeric extraction** is text-based (tables are kept as Markdown inside passages); there is no dedicated numeric solver.
- **Knowledge graph, multi-hop research agent, manufacturer-change alerts**: not built. Scheduled sync + change detection provide
  the data foundation for the latter.
- **Database connectors**: not implemented (only files, websites, REST JSON/XML). **Other embedding providers / web-search
  services**: not integrated; the Providers page says so.
- **Conflict detection** is model-assisted (instruction + comparison conflicts list), not an independent rule engine.
- **UI language**: interface is English; content (questions, answers, passages) supports Arabic with automatic RTL per block.
- **Multi-turn memory**: short follow-ups reuse the previous question for retrieval only; the model is not given chat history.
- **TLS** is not terminated by the platform; use a reverse proxy for network exposure.
- Website marketing claims (e.g. market share, accuracy percentages) are indexed like any page and can be quoted as
  "what the page says"; they are not verified specifications. Prefer manuals/datasheets for compliance work.
