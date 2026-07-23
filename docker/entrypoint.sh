#!/usr/bin/env bash
#
# Container entrypoint (§16). Selects the process role from the first argument
# so one image serves the API, the Celery worker, and Celery beat.
#
#   api    → run `alembic upgrade head`, then uvicorn        (the sole migrator)
#   worker → run the Celery worker (all queues)              (no migration)
#   beat   → run the Celery beat scheduler                   (no migration)
#
# Only the `api` role migrates. When running more than one API replica behind a
# proxy, run migrations from a single one-shot job/release step first and start
# the replicas with migrations disabled (RUN_MIGRATIONS=0). Additive
# (expand-contract) migrations make a brief overlap safe (§15).

set -euo pipefail

ROLE="${1:-api}"
APP_PORT="${APP_PORT:-8000}"
WEB_CONCURRENCY="${WEB_CONCURRENCY:-2}"
CELERY_QUEUES="${CELERY_QUEUES:-default,media,sync,analytics}"
CELERY_CONCURRENCY="${CELERY_CONCURRENCY:-2}"

run_migrations() {
    if [[ "${RUN_MIGRATIONS:-1}" == "1" ]]; then
        echo "[entrypoint] running alembic upgrade head"
        alembic upgrade head
    else
        echo "[entrypoint] RUN_MIGRATIONS=0 — skipping migrations"
    fi
}

case "$ROLE" in
    api)
        run_migrations
        echo "[entrypoint] starting uvicorn on :${APP_PORT} (${WEB_CONCURRENCY} workers)"
        exec uvicorn app.main:app \
            --host 0.0.0.0 \
            --port "${APP_PORT}" \
            --workers "${WEB_CONCURRENCY}" \
            --no-server-header
        ;;
    worker)
        echo "[entrypoint] starting celery worker (queues: ${CELERY_QUEUES})"
        exec celery -A app.workers.celery_app worker \
            --loglevel info \
            --concurrency "${CELERY_CONCURRENCY}" \
            -Q "${CELERY_QUEUES}"
        ;;
    beat)
        echo "[entrypoint] starting celery beat"
        exec celery -A app.workers.celery_app beat --loglevel info
        ;;
    *)
        # Any other argument is treated as a raw command (e.g. a one-shot
        # `alembic ...` or a shell for debugging).
        echo "[entrypoint] exec: $*"
        exec "$@"
        ;;
esac
