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
