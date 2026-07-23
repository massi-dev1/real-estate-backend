# syntax=docker/dockerfile:1
#
# Production image for the Real Estate backend (§16).
#
# One image, three roles — the same artifact runs the API, the Celery worker,
# and Celery beat; the entrypoint (docker/entrypoint.sh) selects the role from
# its first argument. Build once, promote the same digest to every service.
#
# Multi-stage: the builder installs the locked venv with uv; the runtime is a
# slim image carrying only the venv + the libvips runtime libs pyvips needs.

# ---- Stage 1: builder ------------------------------------------------------
FROM python:3.13-slim AS builder

# uv is copied from its official distroless image (pinned by tag).
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Install dependencies first (cached until the lockfile changes), then the
# project. --frozen fails if uv.lock is out of date; --no-dev omits test/lint
# deps from the production image.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- Stage 2: runtime ------------------------------------------------------
FROM python:3.13-slim AS runtime

# libvips is a native dependency of pyvips (§8.2 image pipeline); the -dev
# headers are only needed at build time, so the slim runtime carries just the
# shared library. curl backs the container HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libvips42 curl \
    && rm -rf /var/lib/apt/lists/*

# Run as a non-root user (§15/§10 — never run the app as root).
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src
COPY --from=builder --chown=app:app /app/alembic /app/alembic
COPY --from=builder --chown=app:app /app/alembic.ini /app/alembic.ini
COPY --chown=app:app docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_PORT=8000

USER app
EXPOSE 8000

# Liveness probe hits the dependency-free /healthz (§14). Readiness (/readyz,
# which touches PG+Redis) is gated by the orchestrator, not the container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${APP_PORT}/healthz" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["api"]
