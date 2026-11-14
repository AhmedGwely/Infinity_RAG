# Operations & deployment guide

## 1. Prerequisites
Windows 11, Docker Desktop (WSL2 backend) running, ~15 GB free disk (images + 2.3 GB model + data), and your existing
Qdrant container (`infinity-qdrant`) running with REST port 6333 published to the host.

Recommended Docker Desktop settings (Settings → Resources → WSL integration / `.wslconfig`): **memory limit 10–12 GB**.
Typical steady-state use: embedder ~3 GB, worker ~0.6 GB, api ~0.4 GB, PostgreSQL ~0.2 GB, Redis ~0.05 GB.

## 2. First start
```powershell
.\start.ps1
```
What it does, in order:
1. Verifies Docker is running.
2. **Read-only** inspection of `infinity-qdrant`: image, storage mounts, restart policy, published port, API-key
   requirement, and the `rd_engineering` collection (point count, vector size). It then takes a one-time snapshot
   *inside Qdrant* (non-destructive) as a safety net. Use `-SkipSnapshot` to skip.
3. Creates `.env` or **appends only missing keys** (secrets are generated; your Gemini key/model are reused).
4. Chooses a free web port (default 8088; the only port published) and runs `docker compose up -d --build`.
5. Waits for health, prints the first admin credentials once (also stored in `.env`), opens the browser.

On first start the worker automatically **mirrors** the existing collection's payload text into PostgreSQL
(read-only against Qdrant: no vectors are read, written or recomputed). This enables keyword/exact search, visibility
control and citation lookup for your existing points. Watch it under **Ingestion** / **System & logs**.

## 3. Surviving a reboot
All services use `restart: unless-stopped`. Make sure of two things:
- Docker Desktop → Settings → General → **Start Docker Desktop when you sign in**.
- Your Qdrant container restarts too. `start.ps1` warns if it has no restart policy; fix with
  `docker update --restart unless-stopped infinity-qdrant` (changes only the restart setting, not data).

If Qdrant comes up after the platform, nothing breaks: the worker retries every 20 s, queued jobs wait, and jobs that
failed only because a dependency was still starting are re-queued automatically (up to 3 attempts).
PostgreSQL, uploads, Redis AOF and the model cache live in named Docker volumes, so data persists.

## 4. Daily operation
| Task | How |
|---|---|
| Open the app | http://localhost:8088 (or `WEB_PORT` in `.env`) |
| Stop / start | `.\stop.ps1` / `.\start.ps1` |
| Status | `docker compose ps` |
| Logs | `docker compose logs -f api worker embedder` |
| Update after code changes | `.\start.ps1` (rebuilds) |
| Add users | Users page (admin) |
| Change AI model / key | Providers page (admin); no restart needed |
| Verify everything | `python scripts/verify_platform.py` (add `--llm` to test a real Gemini answer) |

## 5. Security model
- Session cookie (HttpOnly, SameSite=Lax) backed by a server-side session table (revocable); scrypt password hashes;
  login and API rate limiting in Redis; CSRF header enforced on all state-changing calls.
- Roles: **admin** (everything), **engineer** (upload, compare, report; reads public+internal), **viewer** (public only).
  Documents and sources carry a visibility level (public / internal / confidential); it is enforced from PostgreSQL on
  every search, so changing visibility takes effect immediately without re-indexing.
- Provider API keys and source credentials are encrypted (Fernet, key derived from `APP_SECRET_KEY`) and are never
  returned to the browser (only a masked preview). **Keep `APP_SECRET_KEY`** — losing it makes stored keys unreadable.
- Website/API sources are read-only (GET), honour robots.txt, wait ≥1 s between requests, validate every redirect and
  refuse private/loopback/link-local addresses (SSRF guard) unless `ALLOW_PRIVATE_NETWORK_SOURCES=true`.
- Everything security-relevant is written to the audit log (System & logs).
- The platform publishes plain HTTP on localhost. For a company server put it behind a TLS reverse proxy and set
  `COOKIE_SECURE=true`.
- `.env` holds secrets: never commit it (it is git-ignored and excluded from Docker images).

## 6. Backup & recovery
```powershell
.\backup.ps1        # -> backups\<timestamp>\ (postgres.sql, uploads.tar, qdrant-<collection>.snapshot, env.backup)
```
Restore PostgreSQL: `docker compose exec -T postgres psql -U infinity -d infinity < backups\<ts>\postgres.sql`
(from cmd.exe or Git Bash; in PowerShell use `Get-Content -Raw ... | docker compose exec -T postgres psql ...`).
Restore uploads: `docker run --rm -v infinity_uploads:/data -v <backup dir>:/in alpine tar -xf /in/uploads.tar -C /data`.
Restore Qdrant: use Qdrant's snapshot recovery for the collection (`PUT /collections/<name>/snapshots/upload` or place
the snapshot in the container's snapshot directory and recover). The snapshot is only needed if the Qdrant volume is lost.
The index is rebuildable from stored files, but a snapshot avoids hours of CPU embedding.

## 7. Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| `start.ps1` says Qdrant port not published | Recreate your Qdrant run command with `-p 6333:6333`, or set `PLATFORM_QDRANT_URL` |
| System page: Embedding service "loading" | First start is downloading BGE-M3; wait, uploads queue meanwhile |
| Search works but "semantic unavailable" banner | Embedder or Qdrant not ready; keyword/exact search keeps working |
| Answer fails with "check provider settings" | Providers page → test connection; the model name or key is wrong |
| Port 8088 busy | `start.ps1` picks the next free port and records it in `.env` |
| Forgot admin password | Another admin resets it; or stop the stack and set a new `BOOTSTRAP_ADMIN_PASSWORD` only works when **no users** exist |
| Out of memory | Lower `EMBEDDER_THREADS`, raise Docker Desktop's memory limit, avoid concurrent large uploads |

## 8. Moving to a company server
Same Compose file on a Linux/Windows server: copy the repo, run `docker compose up -d --build` (Linux has no
`start.ps1`; create `.env` from `.env.example` and set the secrets by hand). Use the bundled Qdrant
(`docker compose --profile bundled-qdrant up -d`, `PLATFORM_QDRANT_URL=http://qdrant:6333`) or point at a managed one.
With a GPU server you can swap the embedder image/device and raise `EMBEDDER_THREADS`; the Ollama provider can target
a GPU host via `PLATFORM_OLLAMA_URL`.
