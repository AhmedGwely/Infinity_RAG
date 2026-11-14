"""Pre-flight database check, run before migrations and before the worker starts.

    python -m infinity.dbcheck [--wait SECONDS]

It performs a REAL TCP connection with the effective runtime credentials (the same ones Alembic and the app use), so it
proves password authentication - unlike `psql` over the local socket inside the postgres container, which is `trust`.
Passwords are never printed. Exit codes: 0 ok, 2 PostgreSQL unreachable, 3 authentication failed, 4 other error.
"""
from __future__ import annotations

import sys
import time

import psycopg
from sqlalchemy.engine import make_url

from .config import get_config


def describe() -> dict:
    url = make_url(get_config().database_url)
    return {"user": url.username, "host": url.host, "port": url.port or 5432, "database": url.database,
            "password_set": bool(url.password), "password_length": len(url.password or "")}


def _connect(url):
    return psycopg.connect(host=url.host, port=url.port or 5432, user=url.username, password=url.password,
                           dbname=url.database, connect_timeout=5)


def main(argv: list[str]) -> int:
    wait = 60
    if "--wait" in argv:
        wait = int(argv[argv.index("--wait") + 1])
    url = make_url(get_config().database_url)
    info = describe()
    print(f"[dbcheck] target {info['user']}@{info['host']}:{info['port']}/{info['database']} "
          f"(password configured: {info['password_set']}, length {info['password_length']})", flush=True)
    deadline = time.monotonic() + wait
    last = ""
    while True:
        try:
            with _connect(url) as conn:
                row = conn.execute("SELECT current_user, current_database(), inet_server_addr()::text, version()").fetchone()
            print(f"[dbcheck] OK  TCP password authentication succeeded: user={row[0]} database={row[1]} server={row[2]}", flush=True)
            return 0
        except psycopg.OperationalError as exc:
            msg = str(exc).strip().splitlines()[-1] if str(exc).strip() else repr(exc)
            if "password authentication failed" in msg or "no password supplied" in msg:
                print(f"[dbcheck] FAIL  {msg}\n"
                      "[dbcheck] PostgreSQL is reachable but REJECTED the password in .env (POSTGRES_PASSWORD).\n"
                      "[dbcheck] Cause: the database volume was created with a different password. POSTGRES_PASSWORD is only applied\n"
                      "[dbcheck]        when the volume is first created, so changing .env later does not change the stored role password.\n"
                      "[dbcheck] Fix (keeps all data):  .\\scripts\\db-doctor.ps1 -Fix   - or restore the original .env.", file=sys.stderr, flush=True)
                return 3
            last = msg
            if time.monotonic() >= deadline:
                print(f"[dbcheck] FAIL  PostgreSQL not reachable after {wait}s: {last}", file=sys.stderr, flush=True)
                return 2
            time.sleep(2)
        except Exception as exc:  # noqa: BLE001 - report anything else clearly instead of looping
            print(f"[dbcheck] FAIL  {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 4


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
