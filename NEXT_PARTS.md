# Remaining Build Parts — Prompts

Read this file before starting the next part. Do **only the next undone part** —
check the **Build progress log** in `CLAUDE.md` to see which was last completed,
then paste that part's prompt below to Claude as-is (or lightly adapt).

General rules that apply to every part (already in `CLAUDE.md`, repeated here
so they're not missed): one part at a time; query `graphify-out/` before
searching from scratch; routers never touch the DB; every repository method
takes `tenant_id`; `*Out`/`InputSchema` schema discipline; RFC 9457 errors;
camelCase wire / snake_case Python; cursor pagination; anything >~200ms goes
to a Celery worker; run `uv run pytest` + `uv run ruff check` + `uv run mypy`
all green, update the Build progress log, then commit — code review and graph
updates stay manual (done by the user, not automatically). Follow the
patterns already established in Parts 1–16 (capture-trunk honeypot/rate-limit
for public forms, stateless HMAC tokens via `core/security.py` for
non-authenticated multi-step flows, `run_scoped_many` for Beat sweeps,
boundary-accessor methods instead of cross-module model/repository imports).

---

## All module parts (§8.1–§8.18) shipped

Parts 1–24 are complete — every numbered §8 subsection is built (see the
**Build progress log** in `CLAUDE.md`). No module prompts remain in the
queue.

Candidate next phases beyond the module list (discuss with the user before
starting one):

- **§11** — background-workers detail, if any sweep/queue coverage is missing.
- **§14** — observability (metrics, tracing, structured-log dashboards, alerts).
- **§15** — CI/CD (pipeline, migration gating, test/lint/type gates in CI).
- **§16** — hosting/deployment (app Dockerfile, on-demand-TLS wiring for verified
  domains, prod compose/orchestration, secrets management).

Standing deferrals to fold in once prerequisites exist: real provider adapters
(portal, billing, e-signature, AI) once credentials are available; MJML
notification templates; SMS/WhatsApp adapters; Meilisearch at listing scale;
the AI chat assistant + behavioral recommendations (§8.18 "ship later").
