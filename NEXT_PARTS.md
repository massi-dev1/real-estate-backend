# Remaining Build Parts — Prompts

Read this file before starting the next part. Do **only the next undone part** —
check the **Build progress log** in `CLAUDE.md` to see which was last completed,
then paste that part's prompt below to Claude as-is (or lightly adapt).

**Where we are:** Parts 1–24 shipped every §8 feature module (§8.1–§8.18).
Parts 25–33 are the **production-hardening phases** that close the cross-cutting
blueprint concerns deferred out of the module parts: §10 security, §11 caching,
§13 test infra, §14 observability, §15 CI/CD, §16 deployment, §18 checklist.
**Parts 25 (deployment), 26 (CI/CD), 27 (observability), 28 (edge security) and
30 (data-protection primitives) are done.**
The full plan lives at
`~/.claude/plans/okay-build-a-workflow-lucky-crayon.md`.

**Sequence (dependency-ordered):** 25 → 26 → 27 → 28 → 30 → **29** → 31 → 32 → 33.
(Part 30's crypto helper now exists — `core/crypto.py`'s `EncryptedString` —
so Part 29 can field-encrypt the MFA secret with it.)

General rules that apply to every part (already in `CLAUDE.md`, repeated here so
they're not missed): one part at a time; query `graphify-out/` before searching
from scratch; routers never touch the DB; every repository method takes
`tenant_id`; `*Out`/`InputSchema` schema discipline; RFC 9457 errors; camelCase
wire / snake_case Python; cursor pagination; anything >~200ms goes to a Celery
worker; new `AppError` subclasses in `core/exceptions.py`; new config fields in
`core/config.py` with the fail-fast (required) or documented-dev-default pattern;
offline-safe defaults for any third-party seam (stub/flag off with no creds, same
stance as the AI/billing stubs). Run `uv run pytest` + `uv run ruff check` +
`uv run ruff format --check` + `uv run mypy` all green, update the Build progress
log, then commit — code review and graph updates stay manual (done by the user).

---

## Part 29 — Auth hardening: lockout, MFA, breach-check, OAuth seam (§7.1, §10.3)

Add the auth differentiators (extends `modules/auth` + `modules/users`; depends
on Part 30's `core/crypto.py`). **Account lockout / backoff** (§7.1): Redis
per-account + per-IP failed-attempt counters with exponential backoff after 5
failures, resetting on success — the 401 stays generic (no enumeration); fits
`AuthService.login`. **Breached-password check** (§10.3): HIBP k-anonymity range
API via `httpx` on register + password-change, rejecting known-breached passwords
with a clear 422, **fail-open** on an HIBP outage (never block signup on a
third-party being down — document it). **MFA/TOTP** (§7.1): the `users.mfa_secret`
column already exists (reserved); add `pyotp`, an enroll (QR provisioning URI) →
verify → **enforce for admin/agent/team_lead at login** flow (a second-factor
step returning a short-lived MFA-pending token), and **field-encrypt the TOTP
secret at rest** using Part 30's `EncryptedString`. Migration for any new columns
(e.g. `mfa_enabled`). **OAuth social login — seam only** (§7.1):
`integrations/auth_oauth/` with a provider protocol + a Google adapter via
`authlib`, constructed only when `oauth_google_client_id`/`_secret` are set
(offline-safe default; without creds the routes return a clear "not configured" —
flips on when creds arrive). **Session-list endpoint** (§10.3): `GET /me/sessions`
+ `DELETE /me/sessions/{id}` ("log out other devices") over the existing
`auth_sessions` table. Tests: lockout after 5 fails + unlock on backoff expiry;
breached password rejected (mock HIBP); TOTP enroll→verify→enforced-login
round-trip + encrypted-secret-unreadable-in-DB; OAuth routes "not configured"
without creds; session list + revoke. Update the log and commit.

---

## Part 31 — Transactional outbox + outbound webhooks (§12, §8.14, §10.9)

Build the reliability + integration differentiators. **Outbox pattern** (§12): an
`outbox` table (tenant-RLS) written **in the same transaction** as the triggering
row; a Beat-driven relay worker drains it to Celery/notifications with
at-least-once + idempotency, so a broker hiccup between commit and enqueue can't
drop a lead notification. Migration + an outbox service (`core/events.py` or a
small module). **Retrofit the critical `lead.created` speed-to-lead side-effect**
(currently a post-commit hook) to route through the outbox; leave
lower-criticality post-commit hooks as-is (documented). **Outbound webhooks
module** (`modules/webhooks`, §8.14/§10.9): tenant-owned webhook endpoints (URL +
HMAC secret), an event-subscription model, a signed (HMAC-SHA256 header) delivery
worker with retries/backoff + a **per-endpoint circuit breaker** (reuse the
pattern already built in `modules/syndication`), and a delivery log; fired from
domain events (lead.created, listing.published, deal.closed). **SSRF guard**
(§10.4): `core/net.py` validates webhook target URLs (scheme/host, resolve DNS,
block private/loopback/link-local ranges via `ipaddress`, no redirects-to-private)
before any outbound delivery — reusable for any future user-supplied-URL fetch.
Tests: a lead created in a transaction that then fails to enqueue is still
delivered by the relay on the next tick (outbox durability); a signed webhook
delivers + verifies, retries on 5xx, opens the circuit after N fails; the SSRF
guard rejects `169.254.169.254`, `localhost`, `10.0.0.0/8`, etc. Update the log
and commit.

---

## Part 32 — Caching & performance (§11)

Build the CDN-absorption + latency story. **`core/cache.py`** — a generic
`cache_aside(key, loader, ttl)` helper with the versioned-key scheme
`cache:{tenant}:{entity}:{id}:{v}` and version-bump invalidation on write (reuse
the existing Redis client + the codebase's degrade-open stance). Apply it to the
hot reads §11 names: `GET /site/config` (5 min), public content pages/nav
(5 min), search facet counts (60s), map clusters per viewport-hash (60s) —
invalidating on the relevant writes. **HTTP caching** (§11): `ETag`/
`Last-Modified` on public listing/detail/content GETs + `Cache-Control: public,
s-maxage=60` so the CDN absorbs anonymous traffic; return `304` on
`If-None-Match` (a small `core/http_cache.py` helper + response wiring on the
public routers). Tests: a second identical read is served from Redis (loader not
called / hit-ratio metric increments); a write bumps the version and the next
read misses; `If-None-Match` with a matching ETag → 304; `Cache-Control` present
on public GETs. Update the log and commit.

---

## Part 33 — Test infrastructure & §13 conformance + §18 checklist close-out

Bring the suite to the §13 pyramid and pass the §18 production gate — the
**definitive close-out**. Add **testcontainers** so the suite self-provisions
Postgres+PostGIS+Redis(+MinIO) instead of assuming a hand-started stack (keep the
reuse-a-running-stack fast path as an opt-in for local speed). Add **factory_boy**
model factories for the core entities (tenant, user, listing, lead, deal…),
introduced alongside the existing `make_*` helpers and migrated into the
highest-churn suites — don't rewrite all ~350 tests at once. Add **hypothesis**
property-based tests for the money/commission math in `test_transactions.py` (the
one §13 spot example-based tests are called out as insufficient). Add a
**parametrized tenant-isolation harness**: a single module-registry-driven test
asserting, for every registered resource type, that tenant B's admin gets 404 on
tenant A's object — "automatic for new modules" per §13, replacing the
hand-written per-file coverage. Add **`pytest-cov` + a coverage gate**
(`[tool.coverage.report] fail_under` ~85% on `modules/` + `core/`) and wire the
`--cov` run into the Part 26 CI job. Make the **migration upgrade/downgrade
test** explicit in CI. Finally, **walk the §18 production-readiness checklist**:
for each item, confirm done (naming the part that did it) or record a conscious
written waiver in `CLAUDE.md`. After this part, "what's left" is only the
credential-gated deferrals below. Update the log and commit.

---

## Standing deferrals after Part 33 (credential-gated only — NOT backlog)

These flip on when the external account/credential exists — no further code
architecture required: real Stripe/Chargily, real portal, real e-signature, real
AI provider adapters (seams done); a live OAuth client + Turnstile secret +
SMS/WhatsApp provider (seams done); MJML notification templates; Meilisearch at
>50k listings; the AI chat assistant + behavioral recommendations (§8.18 "ship
later").
