#!/bin/sh
set -e
case "$1" in
  api)
    # 1) Prove TCP password authentication (waits for PostgreSQL; fails fast and clearly on a wrong password).
    python -m infinity.dbcheck --wait "${DB_WAIT_SECONDS:-90}"
    # 2) Apply migrations exactly once. `set -e` aborts the container with a clear error if they fail - no silent retry loop.
    alembic upgrade head
    # 3) Serve.
    exec uvicorn infinity.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips='*'
    ;;
  worker)
    python -m infinity.dbcheck --wait "${DB_WAIT_SECONDS:-90}"
    exec python -m infinity.worker
    ;;
  *)
    exec "$@"
    ;;
esac
