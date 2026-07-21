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

## Part 20 — Portal syndication (§8.14)

New `integrations/portals/` (note: outside `modules/`, per the blueprint's
own naming — this is an integration layer, not a tenant-facing feature
module; keep it out of the RBAC/portal-router conventions that assume a
feature module and instead treat it as infrastructure triggered by listing
events).

- One adapter class per target portal behind a common interface:
  `push(listing) / update(listing) / remove(listing)`. Ship with at least
  one real or clearly-stubbed adapter (if no real local portal API/creds
  are available, build one adapter against a documented mock contract and
  say so explicitly — don't fabricate a fake integration and call it done).
- Per-portal sync-state table (tenant-RLS): `listing_id`, `portal_key`,
  `remote_id`, `last_pushed_at`, `last_status`, `last_error`,
  `retry_count`.
- Triggered from listing lifecycle events (`publish`/`update`/`archive` —
  reuse the same post-commit-hook enqueue pattern Part 7's alert-matching
  and Part 14/15's processing tasks use, don't poll). Retries with
  exponential backoff (Celery's built-in retry/backoff, queue `sync` —
  it already exists in `celery_app.py`'s queue set specifically for this).
- Circuit breaker per portal-tenant pair so one broken adapter doesn't
  retry-storm forever — a simple "N consecutive failures → pause syncing,
  surface in portal UI" is enough for v1, no need for a generic breaker
  library.
- Feed generation: stable per-tenant URLs serving XML/CSV of published
  listings (`GET /feeds/{tenant}/{format}` or similar — check how
  `seo_router`'s sitemap is exposed and mirror that pattern for
  discoverability/auth-none). Feeds are pull-based so they need no
  Celery trigger, just a live query at request time (reuse the existing
  public-listing query builder, don't duplicate it).
- Portal admin: `/portal/syndication` — which portals are enabled per
  tenant (tenant `settings.syndication.*`, same defensive-JSONB-settings
  pattern used elsewhere), manual re-push action, sync-state visibility
  per listing. New permission if warranted (likely reuses
  `LISTING_MANAGE` — syndication is a listing concern, not a new domain).

Update Build progress log in the established style.

---

## Part 21 — Analytics & reporting (§8.15)

New `modules/analytics`.

- `POST /events` public, batched, anonymous-ok: `listing_view`, `search`,
  `favorite`, `form_start`, `form_submit`, `page_view`. Validate a tight
  allowlisted event-type enum + a small typed payload per type (don't
  accept arbitrary JSONB from anonymous clients — that's an abuse/storage
  vector). Rate-limit like every other public surface (own bucket).
  Respect §10.12: no ingestion for non-consenting sessions if cookie
  consent has landed by this point (check whether §8.17 compliance has
  shipped yet; if not, note the consent-gate as a TODO wired in when
  compliance lands, don't block this part on it).
- Append-only `analytics_events` table — **monthly partitions** per the
  blueprint (native Postgres declarative partitioning on a
  `created_at`/month key; write a migration that creates the parent +
  the current and next month's partitions, and a Beat job or documented
  runbook step that creates future partitions ahead of time — don't
  let inserts fail because a partition doesn't exist yet).
- Nightly Beat rollup jobs (queue `analytics`) aggregating raw events into
  `listing_stats_daily`, `lead_funnel_daily`, `source_performance_daily` —
  per-tenant via `run_scoped_many`, idempotent re-aggregation for a given
  day (upsert on `(tenant_id, listing_id/source, day)`, not insert-only,
  so a re-run doesn't double-count — same idempotency stance as
  `flag_stale_listings`/blog's scheduler).
- Raw event pruning: Beat job deleting `analytics_events` older than 90
  days (drop whole old partitions instead of row-by-row DELETE — much
  cheaper, and it's the whole reason to partition by month).
- Dashboards read **only from rollup tables**, never raw events — portal
  endpoints for traffic/top-listings, lead volume/source/conversion, agent
  performance (can lean on existing `AgentsService` stats boundary from
  Part 9 rather than duplicating), and a seller-dashboard endpoint
  (views/saves/inquiries per listing — likely `/me` or portal-scoped
  depending on whether sellers have accounts yet; check Part 10's `/me`
  prefix and extend it if sellers are just tenant users, otherwise scope
  this to the portal side and note the seller-portal gap as deferred).
- New permission if a portal-wide reporting view needs gating beyond
  existing scope rules (`ANALYTICS_VIEW` or reuse tenant-wide role checks
  — use judgement based on what's already in the permission matrix).

Update Build progress log in the established style.

---

## Part 22 — Tenant administration & billing (§8.16)

Extends the existing `modules/tenants` (platform-level, from Part 2) —
don't create a new module, this is squarely tenant lifecycle/billing.

- Tenant lifecycle beyond what Part 2 built (create/suspend/activate
  already exist): trial state + trial-expiry handling, full **offboard**
  flow (export then scheduled deletion — reuse/extend whatever DSR export
  shape §10.12/compliance defines if that part has landed; if not, build
  a straightforward "dump tenant's rows across modules to a downloadable
  archive" job and note alignment-with-compliance-export as a later
  reconciliation item).
- Domain management extension: DNS verification (TXT record challenge,
  stored + checked via a Beat sweep or on-demand verify endpoint) and
  automatic TLS — actual Caddy on-demand-TLS or Cloudflare-for-SaaS
  wiring is infrastructure/ops work outside the FastAPI app; build the
  domain-verification *data model and API* (verification token, status,
  verified_at) and document what the ops-side TLS wiring needs to consume
  from it, rather than trying to shell out to infra from the app.
- Plans & quotas: `max_listings`, `max_agents`, storage GB, monthly
  emails — enforced **at write-time** in the relevant services (listing
  create checks `max_listings`, agent-profile create checks `max_agents`,
  media upload checks storage GB via a running total, not a full
  recompute scan). Surface current usage + limits in `GET /site/config`
  (already exists from Part 2 — extend it, don't add a parallel endpoint).
  Over-quota is a 403 `problem+json` (`type: quota-exceeded`, matching
  §9's own worked example).
- Billing: `BillingProvider` interface (Stripe primary, Chargily for DZ —
  build the interface + a Stripe implementation if credentials are
  realistically available in this environment; otherwise implement the
  interface with a clearly-labeled stub/sandbox provider and say so, same
  "design the seam, defer the live integration" stance as Part 19's
  e-signature and Part 20's portal adapter). Subscription lifecycle
  webhook handling per §10.9 (HMAC/signature verification, ±5min
  freshness, idempotent by event id, dedicated handling — mirrors the
  webhook hardening rules verbatim). Dunning: grace period then auto-
  suspend via Beat sweep (reuses Part 2's existing suspend machinery,
  don't reimplement it).
- Platform admin: cross-tenant metrics endpoint (aggregates from Part 21's
  rollups if that part has landed; otherwise a light live-query version,
  noted for later migration to rollups), and **impersonation** ("login as
  tenant admin") — must be audit-logged (§10.11 — if `audit_log` doesn't
  exist yet, this part needs to add at least the minimal append-only
  table for this one use, and Part 23/compliance can broaden it later),
  time-boxed (short-lived special-purpose token, not a normal session),
  and the frontend contract needs a clear "impersonation active" signal
  in the token/response so a banner can be shown — this part only needs
  to guarantee that signal exists, not build the banner itself.

Update Build progress log in the established style.

---

## Part 23 — Compliance module (§8.17)

New `modules/compliance`.

- Consent records: what was consented to, when, and proof (IP, user
  agent, timestamp, version of the policy consented to — ties into Part
  14's versioned legal pages, reference `legal_pages.id`+version rather
  than duplicating policy text). Write path: anywhere consent is
  currently collected implicitly (saved-search double opt-in from Part
  10, any cookie-consent banner) should start writing a consent record
  through this module — audit what already exists and wire it, don't
  just build the table and leave nothing populating it.
- Cookie-consent config per tenant: which categories exist (necessary/
  analytics/marketing), tenant-configurable copy, default state. If Part
  21 (analytics) has landed, this part should make analytics ingestion
  actually check consent state for non-anonymous/cookie-bound sessions —
  close the loop Part 21 was told to leave as a TODO.
- Data-subject requests (§10.12): `GET /me/export` (JSON + files —
  aggregate the user's rows across every module that has `user_id`/
  `contact_id` columns; this will necessarily touch many modules, but
  should be read-only fan-out through each module's own service/boundary
  method, e.g. `LeadsService.export_for_contact`, not raw cross-module
  table reads) and `DELETE /me` (soft-delete → 30-day purge Beat job,
  cascading with anonymization where business records must legally
  persist — e.g. a closed deal's commission record probably survives
  anonymized, a pending lead does not; use judgement per data type and
  document the calls made).
- Retention policy Beat jobs: anonymize lost leads after 24 months
  (extends Part 8's leads module — add the sweep there or call into it
  from here via a boundary method, don't reach into leads' repository
  directly), prune raw analytics at 90 days (Part 21 should already have
  this — if so, this part just confirms/documents it under the
  compliance umbrella rather than duplicating).
- Audit access reports: a portal endpoint surfacing `audit_log` entries
  (from Part 22, or built fresh here if Part 22 hasn't landed yet —
  coordinate whichever part ships first to add the minimal table, and
  have the other part extend it) filtered/exportable for compliance
  review.
- Legal page versioning already exists (Part 14) — this part just needs
  to reference it, not rebuild it.

Update Build progress log in the established style.

---

## Part 24 — AI features (§8.18, design for now, ship later)

Per the blueprint: **the API surface stays stable while implementations
improve.** This part is explicitly scoped to be thin — build the seam,
not a fully-tuned AI product.

- `integrations/ai/` — provider-agnostic interface (e.g. an
  `AITextProvider`/`AIProvider` abstraction), with a concrete
  implementation behind whichever provider is realistically available in
  this environment (note credentials/provider choice explicitly rather
  than assuming one). Everything AI-specific isolated here — no module
  should import a specific AI SDK directly.
- `POST /listings/{id}/generate-description` (portal, gated by existing
  `LISTING_MANAGE`): drafts the i18n description from the listing's
  structured fields (title, property type, area, rooms, features,
  location) via the provider, returns a **draft the agent must explicitly
  save** — never auto-persists the AI output over the agent's own copy.
  Treat this like any other >200ms external call: it's request-time
  (the agent is waiting for a draft to edit), so keep it synchronous but
  apply a sane timeout + graceful error (provider failure → clear 502/503
  problem+json, not a hang).
- Lead-scoring model seam: don't replace Part 8's rules-based score this
  part — add the interface point (e.g. a `LeadScorer` protocol the
  current rules implementation already satisfies) so a model-based scorer
  can be swapped in later without touching call sites. No model training
  in this part.
- AI chat assistant + behavioral recommendations: per the blueprint these
  are explicitly "ship later" — this part should at most stub the API
  contract (route shape, request/response schemas) if there's clear
  value in freezing the contract now, but implementing full RAG chat or a
  recommendation engine is out of scope unless the user asks to expand
  this part. Default to *not* building these unless prompted — flag them
  as available to scope into a future part instead of guessing at an
  implementation.

Update Build progress log in the established style. This is a good point
to note that all of §8's numbered subsections are now covered — check with
the user about polishing/deployment work (§11 background workers detail if
anything's missing, §14 observability, §15 CI/CD, §16 hosting) as candidate
next phases beyond the module list.
