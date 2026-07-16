# Real Estate Backend — Project Instructions

Multi-tenant real estate agency platform (FastAPI + PostgreSQL/PostGIS + Redis).
The full blueprint is in [project.md](project.md) — follow it. The build plan is
incremental: one part at a time, never everything at once.

**Codebase context:** don't re-read source files to answer questions about the
codebase — query the graphify knowledge graph (`graphify-out/`) first. Only open
source files when editing them or when line-level detail is needed.

## Per-part workflow (MANDATORY — run after finishing every part)

1. Run the full check suite: `uv run pytest` + `uv run ruff check` + `uv run mypy` — all green before a part counts as done.
2. Launch the **code-reviewer agent** on the code written in that part; apply legitimate findings.
3. Update the knowledge graph: run `/graphify ./src --update` (the initial full `/graphify` run was done after Part 1).
4. Update the **Build progress log** below (what was built, key decisions).
5. Commit.

## Architecture rules (from project.md §5 — enforced in review)

- Modular monolith under `src/app/modules/<name>/` with `models.py / schemas.py / repository.py / service.py / router.py`.
- Routers never touch the DB: routers → services → repositories.
- Modules never import another module's `models.py`/`repository.py` — call its service or subscribe to its events.
- Every repository method takes `tenant_id`. No exceptions. Postgres RLS is the safety net, not the only guard.
- API responses use explicit `*Out` schemas (`app.core.schema.OutSchema`); inputs use `InputSchema` (`extra="forbid"`).
- Errors leave the API only as RFC 9457 problem+json (raise `AppError` subclasses from `app.core.exceptions`).
- camelCase JSON on the wire, snake_case in Python; cursor pagination (`app.core.pagination`) for all list endpoints.
- Anything > ~200ms of work goes to a background worker (Celery, from Part 5 on).

## Commands

- Deps: `uv sync` · Add dep: `uv add <pkg>`
- Stack: `docker compose -f docker/docker-compose.yml up -d --wait` (Postgres+PostGIS :5432, Redis :6379, Mailpit UI :8025)
- Run: `uv run uvicorn app.main:app --reload --port 8000`
- Tests: `uv run pytest` (needs the docker stack; uses `realestate_test` DB + Redis db 1)
- Lint/types: `uv run ruff check` · `uv run ruff format` · `uv run mypy`
- Migrations: `uv run alembic revision --autogenerate -m "..."` then hand-review; `uv run alembic upgrade head`
- The app connects as non-superuser `app_user` (RLS applies); Alembic uses `DATABASE_DDL_URL` (postgres role).

## Build progress log

- **Part 1 — Scaffold & core foundation (done, 2026-07-15):** uv project (`src/app` layout), docker-compose (PostGIS 16, Redis 7, Mailpit) with `app_user` role + `realestate_test` DB via initdb script, `core/` (config via pydantic-settings, structlog logging with PII redaction, RFC 9457 exception handlers, request-id + security-headers ASGI middleware, cursor pagination, camelCase schema bases), `/healthz` + `/readyz`, async Alembic wired to `DATABASE_DDL_URL` (no migrations yet — first one lands with Part 2 models). 10 tests green; ruff + mypy strict clean.
- **Part 2 — Database foundation & tenancy (done, 2026-07-15):** `core/database.py` (async engine + session factory, declarative `Base` with naming convention, UUIDv7 PK + timestamp mixins, request-scoped session that commits at the request boundary and runs `SET LOCAL app.tenant_id` via `set_config` when a tenant is resolved, post-commit callback hook), `core/rls.py` (RLS DDL helpers for future tenant-table migrations — policy reads `current_setting` without `missing_ok`, fail-closed), `core/tenancy.py` (`TenantContext`, pure-ASGI `TenantResolutionMiddleware`: Host → tenant with exempt prefixes, 404 unknown / 402 suspended as problem+json, `TenantDep`), `core/security.py` (interim `X-Platform-Key` guard until Part 3 auth), tenants module (global `tenants`/`tenant_domains` tables — deliberately no RLS since the middleware queries them pre-context; platform CRUD + suspend/activate + domain management with primary-domain rules; Redis-cached `DomainTenantResolver` degrading to DB on Redis failure), public `GET /site/config`, first migration (hand-written, tested via downgrade/upgrade in conftest). Key decisions: cache invalidation runs via post-commit callbacks (pre-commit invalidation let a concurrent reader re-cache stale state for the TTL — review finding); unique-violation `IntegrityError` maps to 409 (pre-checks race under concurrency — review finding); validation errors strip to `type/loc/msg` (pydantic `ctx`/`input` leak PII and non-serializable objects). 27 tests green incl. real-RLS probe suite; ruff + mypy strict clean.
- **Part 3 — Auth, users & RBAC (done, 2026-07-16):** users module (global `users` table with nullable `tenant_id` — NULL = platform staff; identity RLS policy exposes exactly the caller's partition; Argon2id via pwdlib, timing-safe dummy verify against enumeration; soft delete), auth module (`auth_sessions` table; register/login → access JWT ≤15 min + opaque refresh token in httpOnly path-scoped cookie, SHA-256 at rest; rotation with family-wide revocation on reuse — revocation commits on a *dedicated* session because the 401 rolls back the request transaction; password reset + email verification as single-use hashed Redis tokens consumed with GETDEL; Mailpit SMTP integration, fail-soft), `core/permissions.py` (static role→permission matrix in code, `require()` dependency, tid-claim ↔ resolved-tenant pinning so tokens are useless cross-tenant and cross-plane), platform login replacing the X-Platform-Key stopgap, migration 0002, `scripts/create_platform_admin.py`. Key decisions from review: live access tokens are tracked per user in a Redis set (`auth:jti:all:{id}`) so disable/demote/delete/logout-all/password-reset denylist every outstanding jti *immediately* instead of waiting out the 15-min TTL (review finding — "logout-all" and admin-disable imply immediate effect); the users router orchestrates users+auth services for that revocation since auth already depends on users (no circular import); Redis failures degrade auth checks open for signed ≤15-min tokens but never extend refresh (rows are committed). Deferred: MFA/TOTP, OAuth, lockout backoff, proxy-aware client IP (raw peer recorded until an X-Forwarded-For trust boundary exists). 58 tests green; ruff + mypy strict clean.
- **Part 4 — Listings module (Phase 1 §8: CRUD + workflow + i18n), the first RLS-protected tenant business table:** next.
