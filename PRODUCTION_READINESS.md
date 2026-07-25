# §18 Production-Readiness Checklist — Close-Out

Walked at Part 33 (2026-07-25), the final build part. Every item in
[project.md §18](project.md) is either **done** — naming the part that did it,
so the claim is checkable — or carries a **written waiver** explaining why it
is deliberately not done and what would trigger doing it.

Two things are deliberately *not* here. Nothing is marked done on the strength
of intent: an item that needs an operator action outside this repository
(pointing DNS, buying a certificate, running a restore drill) is a waiver even
where the code side is complete, because a checklist that scores the code and
calls the system ready is exactly the checklist that lets an untested backup
pass for a backup. And no waiver says "later" without saying what it costs and
what unblocks it.

**Legend:** ✅ done · ⚠️ waived (rationale + trigger) · 🔑 credential-gated
(code complete behind a seam, flips on when an account exists)

---

## Security

| Item | State | Evidence / rationale |
| --- | --- | --- |
| TLS + HSTS | ✅ | Caddy terminates TLS with on-demand certs for tenant domains (Part 25); HSTS at the edge *and* in-app (`SecurityHeadersMiddleware`, Part 28), conditional so a `max-age` is never cached from plain-http localhost. |
| Security headers | ✅ | Part 28: CSP split API (`default-src 'none'`) vs. docs, `X-Frame-Options`, `X-Content-Type-Options`, `frame-ancestors`/`base-uri`/`form-action`. Applied to preflight replies too. |
| CORS allowlist | ✅ | Part 28: dynamic per-request, resolved through `tenant_domains` — an origin is reflected only when it and the request host resolve to the *same* tenant. Never `*` alongside credentials. |
| Rate limits | ✅ | Part 28: global per-IP budget above tenant resolution + per-endpoint auth limits; sliding-window log, real `Retry-After`. Per-surface capture limits since Part 8. |
| Argon2id | ✅ | Part 3, via pwdlib; timing-safe dummy verify against enumeration. |
| Refresh rotation + reuse detection | ✅ | Part 3: rotation with family-wide revocation on reuse, committed on a dedicated session so the 401 rollback cannot undo it. |
| MFA for staff | ⚠️ | TOTP enrol/verify/disable ships (Part 29) and privileged roles are *prompted*. Enforcement is deliberately **not** a hard login block: flipping it on would instantly lock out every existing admin including whoever would fix it. **Trigger:** enforce per-tenant after an enrolment grace period, once a tenant's admins have enrolled. |
| RLS enabled + non-owner DB role | ✅ | Part 2 onward; app connects as `app_user`, migrations as `postgres`. Policies verified against real Postgres, including on the partitioned `analytics_events` parent (Part 21). |
| Cross-tenant tests green | ✅ | Part 33: `tests/test_tenant_isolation.py` — registry-driven, 10 resource types × detail/list/unknown-id. Plus `tests/test_factories.py` asserting RLS itself (read, write, unscoped, unknown scope). |
| Input sanitization (nh3) | ✅ | Part 15: allowlist sanitization at write time per locale, `link_rel` forced, `javascript:` blocked by scheme allowlist. |
| Upload magic-byte + re-encode | ✅ | Part 6: HEAD-check real size before buffering, magic bytes vs. declared type, libvips re-encode with `keep="none"` (strips EXIF GPS). |
| SSRF guards | ✅ | Part 31: `core/net.py` + `SsrfProtectedTransport` — validates at registration and every hop/redirect, resolves once and **pins the connection to the validated IP** (closes DNS rebinding). |
| Secrets manager | ⚠️ | Secrets are env-only with fail-fast on missing required values (§10.6, Part 1); no external manager is wired. For a single-VPS v1 that is a defensible boundary — the env file is as protected as the host. **Trigger:** a managed platform or more than one operator with shell access. |
| Webhook signatures | ✅ | Both directions, one convention: inbound billing (Part 22) and outbound deliveries (Part 31) use `t=<unix>,v1=<hmac-sha256>` with freshness checks. |
| Audit log | ✅ | Part 22 (append-only table + platform report), Part 23 (tenant-scoped report pinned to the resolved tenant). |
| pip-audit / bandit in CI | ✅ | Part 26, blocking. Bandit waivers are scoped per-check with written rationale in `pyproject.toml` — never a blanket severity filter. |
| Incident runbook | ⚠️ | **Not written.** Deploy/rollback/restore/offboard steps exist as scattered prose in `CLAUDE.md` and the compose files, which is not a runbook — a runbook is read at 3am by someone who did not write it. **Trigger:** before the first production tenant. This is the largest ops gap on the list. |

## Data

| Item | State | Evidence / rationale |
| --- | --- | --- |
| Nightly backups + WAL | ⚠️ | **Not configured.** No `pg_dump` schedule, no pgBackRest, no archiving. The application is backup-*ready* (all state is in Postgres + object storage, both externally snapshottable) but nothing is scheduled. **Trigger:** before the first production tenant — this and the restore test are the two items that would hurt most. |
| Weekly restore test | ⚠️ | **Not configured**, and blocked by the item above. §13's own line applies: *an untested backup is a rumor.* Recorded as a rumor rather than a checkmark. |
| Retention jobs | ✅ | Part 23: 24-month lost-lead anonymization; Part 21: 90-day raw-analytics prune by partition drop (confirmed under the compliance umbrella, not duplicated). |
| DSR export / delete | ✅ | Part 23: `GET /me/export` fans out across every module holding subject data; `DELETE /me` soft-deletes, force-revokes live tokens, schedules a 30-day purge with per-data-type anonymize-vs-delete judgement. |
| Consent records | ✅ | Part 23: append-only `consent_records` referencing the versioned `legal_pages` row (Part 14) — proof of *what* was consented to, not a copy of the text. Wired to the cookie banner, saved-search opt-in, and the analytics gate. |
| PII-redacting logs | ✅ | Part 1 (structlog redactor) + Part 27 (Sentry `before_send`, with a dedicated header denylist — the log-field list has no `cookie` key, which was a real leak found in review). |

## Reliability

| Item | State | Evidence / rationale |
| --- | --- | --- |
| /healthz + /readyz | ✅ | Part 1; extended in Part 27 with bounded probes. Postgres and Redis gate; broker and storage are reported but non-gating — the API serves fine without object storage, and failing readiness would pull healthy replicas over a dependency they do not need. |
| Zero-downtime migrations | ✅ | Part 25: only the `api` role migrates; `RUN_MIGRATIONS=0` for additional replicas, expand-contract documented. Part 26 proves upgrade→downgrade→upgrade on a clean DB in CI. |
| Outbox for lead events | ✅ | Part 31: `lead.created` is written in the same transaction as the lead and drained by a Beat relay with at-least-once + per-event savepoints. |
| Idempotent tasks | ✅ | Every sweep guards on a status/stamp column (`stale_flagged_at`, `reminder_sent_at`, `sent_at`, `SCHEDULED` filter). Rollups upsert absolute recomputed values rather than incrementing. Plus `Idempotency-Key` on the three money/duplicate-sensitive POSTs (Part 30). |
| Dead-letter alerts | ⚠️ | `task_acks_late=True` is set so a lost worker redelivers, and permanent-vs-transient failure classification is consistent across every external seam — but there is **no dead-letter queue and no alert on one**. A task that exhausts its retries is logged and dropped. **Trigger:** wire alongside the alert rules below; needs a Prometheus/Alertmanager target to fire at. |
| Uptime checks | ⚠️ | `/healthz` and `/readyz` exist to be polled, and metrics are exported (Part 27), but no external checker is configured and **no alert rules are written**. Alert rules are Prometheus-side config, not app code, so they cannot live in this repo alone. **Trigger:** deploy-time, with the monitoring stack. |
| Load test passed | ⚠️ | **Never run.** No k6/Locust scenario exists. Performance work so far is structural (indexes, keyset pagination, cache-aside, CDN `s-maxage`) and unvalidated under load; §17 puts load testing in Phase 4 before big-agency onboarding, which is the honest place for it. **Trigger:** before onboarding an agency with real inventory volume. |

## Quality

| Item | State | Evidence / rationale |
| --- | --- | --- |
| ruff + mypy clean | ✅ | Blocking in CI (Part 26): `ruff check`, `ruff format --check`, `mypy --strict`. |
| Coverage gate | ✅ | Part 33: `fail_under = 85` in `pyproject.toml`, wired into CI with a second `modules/`+`core/` report. Currently **92%**. Note: `concurrency = ["greenlet", "thread"]` was required — without it coverage cannot see code running inside SQLAlchemy's async greenlet and under-reported by ~35 points on service modules. |
| RBAC matrix tests | ✅ | `tests/test_rbac.py` (Part 3), extended by each module part as permissions were added. |
| Migration upgrade test in CI | ✅ | Part 26: a dedicated job runs upgrade→downgrade→upgrade on an empty database, so a migration that only works against a populated schema fails in CI rather than during a production rollback. |
| Staging mirrors prod | ⚠️ | The *image* does — one artifact, three roles, env-only differences (Part 25) — but **no staging environment is deployed**, so nothing is being mirrored. **Trigger:** deploy-time; the compose topology is ready to instantiate twice. |

## Business

| Item | State | Evidence / rationale |
| --- | --- | --- |
| Plan quotas enforced | ✅ | Part 22: write-time reservation against O(1) counters with a `FOR UPDATE` on the usage row (never a recompute scan); over-quota is a 403 `quota-exceeded`. Monthly-email volume is deliberately **soft** (surfaced, not blocked) — hard-gating it would thread `tenant_id` through every send site for a rate concern. |
| Billing webhooks verified | 🔑 | Part 22: signature verification, ±5-min freshness, and `(provider, event_id)` idempotency are **real and tested** — only the outbound checkout call is stubbed. Flips on with a Stripe/Chargily account. |
| Tenant offboard / export path | ✅ | Part 22: suspend → scoped JSON export to the private bucket → 30-day scheduled purge, cancellable. *Open reconciliation:* this whole-tenant export and Part 23's per-subject DSR export were built separately and overlap; noted since Part 22 and still worth unifying. |
| Per-domain email auth (SPF/DKIM/DMARC) | ⚠️ | **DNS records, not code** — nothing in this repository can set them. The app side (per-tenant sending domains) is in place; the records must be published per agency domain at onboarding. **Trigger:** each tenant onboarding — belongs in the runbook above. |
| Sitemaps + structured data | ✅ | Part 7 (sitemap + schema.org `RealEstateListing` JSON-LD), extended with pages (Part 14), posts + RSS (Part 15), guides (Part 17). |
| Analytics rollups feeding dashboards | ✅ | Part 21: nightly rollups; dashboards read **only** rollup tables, never raw events. |

---

## Summary

**37 items: 25 done, 1 credential-gated, 11 waived.**

The waivers cluster in one place, and it is worth naming plainly: **operations,
not application code.** Backups, restore drills, the incident runbook, alert
rules, dead-letter alerting, uptime checks, a staging deployment and a load
test are all things that exist outside this repository, and none of them can
be honestly closed by writing more Python.

The three that should block a first production tenant:

1. **Nightly backups + a verified restore** — the only item on this list whose
   absence is unrecoverable.
2. **The incident runbook** — deploy, rollback, restore, offboard.
3. **Alerting** (uptime + dead-letter + the queue-depth/error-rate rules the
   Part 27 metrics already export).

Everything else is either done, or a deliberate trade with its trigger written
down.
