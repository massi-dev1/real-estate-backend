# Production FastAPI Backend — Multi-Tenant Real Estate Agency Platform

**Scope:** One backend serving many agency websites (multi-tenant SaaS). Non-US market (Algeria / EU / MENA): no MLS feed — listings are created by agents/admins, with optional syndication to local property portals. Designed around the full agency feature map: public site, lead capture, CRM, agent tools, messaging, and back-office.

---

## 1. Verdict: Is FastAPI a Good Idea in Production?

**Yes — FastAPI is a first-rate choice for this exact product.** It is not a toy framework; it powers production systems at Netflix, Uber, Microsoft, and thousands of SaaS companies. Here's the honest assessment:

### Why it fits this project

| Strength | Why it matters here |
|---|---|
| **Async-native (ASGI)** | Real estate sites are I/O-heavy: DB queries, image storage, email/SMS, map APIs, portal syndication. Async handles thousands of concurrent connections per worker. |
| **Pydantic validation** | Every lead form, listing payload, and search filter is validated and typed at the boundary. This kills an entire class of bugs and injection vectors for free. |
| **Auto OpenAPI docs** | `/docs` gives you a live, always-accurate API reference. When you onboard a frontend dev or a new agency integration, the contract is self-documenting. |
| **Dependency injection** | Tenant resolution, auth, DB sessions, and permissions become composable dependencies — the cleanest way to enforce multi-tenant isolation on every route. |
| **Performance** | On par with Node/Go frameworks for I/O workloads (TechEmpower benchmarks). A single modest VPS handles a serious amount of traffic. |
| **Python ecosystem** | You'll want AI features later (lead scoring, AI descriptions, chat assistants — see §8.18). Python is where that ecosystem lives. |

### The honest trade-offs (know them before you commit)

1. **FastAPI is a micro-framework.** Unlike Django, it ships with no ORM, no admin, no auth, no migrations. You assemble those yourself (this document tells you exactly what to pick). That's more setup work up front, but you get a stack with zero dead weight.
2. **Async discipline is required.** One blocking call (e.g., a sync image resize inside a route) stalls the event loop for everyone. Rule: routes only do async I/O; anything CPU-heavy or slow goes to the background worker (§12).
3. **You own the architecture.** FastAPI won't stop you from writing spaghetti. The project structure in §5 exists precisely to prevent that.

**Bottom line:** for an API-first, multi-tenant SaaS with real-time features and future AI plans, FastAPI + PostgreSQL is arguably the strongest stack you can pick in 2026. The alternative worth naming is Django (batteries included, faster CRUD scaffolding) — but you'd fight it on async, WebSockets, and API-first design. Commit to FastAPI.

---

## 2. What We're Building — System Overview

One platform, many agency websites. Each agency (tenant) gets its own domain, branding, agents, listings, and leads — all served by one codebase and one database with strict isolation.

```
                      ┌─────────────────────────────────────────────┐
 agency-a.com  ──┐    │                  EDGE                       │
 agency-b.dz   ──┼──► │  CDN + WAF (Cloudflare) → Nginx/Caddy (TLS) │
 agency-c.fr   ──┘    └───────────────────┬─────────────────────────┘
                                          │
                      ┌───────────────────▼─────────────────────────┐
                      │           FastAPI app (N replicas)          │
                      │  middleware: tenant-resolver → auth → RBAC  │
                      │  REST /api/v1  +  WebSocket /ws             │
                      └──────┬──────────────┬──────────────┬────────┘
                             │              │              │
                    ┌────────▼───┐   ┌──────▼─────┐  ┌─────▼──────────┐
                    │ PostgreSQL │   │   Redis    │  │ Object storage │
                    │  + PostGIS │   │ cache/queue│  │ (S3/R2) + CDN  │
                    │  (+ RLS)   │   │ /rate-limit│  │  photos, docs  │
                    └────────────┘   └──────┬─────┘  └────────────────┘
                                            │
                      ┌─────────────────────▼───────────────────────┐
                      │        Celery workers + beat scheduler      │
                      │  emails/SMS · image processing · alerts ·   │
                      │  portal syndication · reports · lead drips  │
                      └─────────────────────────────────────────────┘
                             │
                    ┌────────▼────────────────────────────────┐
                    │ 3rd parties: email (Brevo/SES), SMS,    │
                    │ maps (Google/Mapbox/OSM), payments,      │
                    │ e-signature, analytics, Sentry           │
                    └──────────────────────────────────────────┘
```

**Design principles**

1. **Modular monolith, not microservices.** One deployable app, internally split into feature modules with clean boundaries. Microservices at this stage would multiply your ops burden for zero benefit. The module boundaries in §5 let you extract a service later *if* you ever need to.
2. **Tenant isolation is enforced in three layers** (middleware, repository queries, and Postgres Row-Level Security) — never in just one (§4).
3. **The API is the product.** The same API serves the public website, the agent dashboard, the admin back-office, and future mobile apps. No logic lives in the frontend.
4. **Everything slow is a background job.** Request → validate → persist → enqueue → respond in <100ms. Emails, image variants, alert matching, syndication all happen in workers.

---

## 3. Tech Stack — Exact Choices and Why

| Layer | Choice | Why (and what I rejected) |
|---|---|---|
| Language | **Python 3.12+** | Perf gains, better error messages, `asyncio` improvements. |
| Framework | **FastAPI** (latest) | See §1. |
| ASGI server | **Uvicorn** workers under **Gunicorn** (or uvicorn `--workers` directly in containers) | Battle-tested process management; 2× CPU cores workers per container. |
| Validation | **Pydantic v2** | 5–17× faster than v1 (Rust core). Also used for settings (`pydantic-settings`). |
| ORM | **SQLAlchemy 2.0 (async)** + **asyncpg** | The industry standard. 2.0 style is fully typed. asyncpg is the fastest PG driver. Rejected: Tortoise/SQLModel (smaller ecosystems, SQLModel lags SQLAlchemy releases). |
| Migrations | **Alembic** | The only serious choice with SQLAlchemy. Autogenerate + hand-review every migration. |
| Database | **PostgreSQL 16+** with **PostGIS** | One database does relational data, JSONB (flexible listing attributes), full-text search, and geospatial queries (radius/polygon search). Do not add MongoDB "for flexibility" — JSONB covers it. |
| Cache / broker / rate-limit | **Redis 7** | One Redis instance covers caching, Celery broker, rate limiting, WebSocket pub/sub, and session blacklists. |
| Background jobs | **Celery 5** + Redis broker + **Celery Beat** | Most mature: retries, scheduling, chains, monitoring (Flower). Alternatives: ARQ/Taskiq are leaner and async-native — fine picks, but Celery's ecosystem wins for a business app. |
| Search | **Postgres FTS first**, **Meilisearch** when listings > ~50k or you need typo-tolerance | Don't run Elasticsearch on day one; it's an ops tax. Meilisearch is a single binary with great relevance out of the box. |
| Object storage | **S3-compatible** — Cloudflare R2 (no egress fees) or MinIO (self-hosted) | Photos/documents never touch your app disk. Presigned URLs for direct upload (§8.2). |
| Image processing | **libvips** via `pyvips` (fallback: Pillow) | 5–10× faster and far less memory than Pillow for resizing gallery photos. Runs in Celery workers only. |
| Auth | **JWT access (15 min) + rotating refresh tokens (httpOnly cookie)**; passwords with **Argon2id** (`argon2-cffi` or `pwdlib`) | Avoid `passlib` (unmaintained). Full design in §7. |
| WebSockets | FastAPI native WS + Redis pub/sub | Powers in-app messaging and live notifications across replicas. |
| Email | **Brevo / Amazon SES / Resend** via provider SDK behind an internal `EmailService` interface | Swap providers without touching business code. |
| SMS / WhatsApp | Twilio / local aggregator behind the same pattern | WhatsApp matters a lot in MENA lead follow-up. |
| Maps & geocoding | **Nominatim/OSM** (free) or Google Geocoding; store lat/lng + geometry in PostGIS | Frontend map rendering is a frontend concern; backend stores/queries geometry and geocodes addresses in workers. |
| Payments (tenant billing) | **Stripe** where available; **Chargily Pay** for Algeria (CIB/EDAHABIA) | Billing the agencies is your revenue; abstract behind a `BillingProvider` interface. |
| HTTP client | **httpx** (async) | For all outbound calls (geocoding, portals, webhooks) with timeouts + retries. |
| Dependency mgmt | **uv** + `pyproject.toml` | 10–100× faster than pip/poetry; lockfile for reproducible builds. |
| Lint / format / types | **Ruff** (lint+format) + **mypy** (strict on `app/`) | Ruff replaces black+isort+flake8 in one tool. |
| Testing | **pytest** + `pytest-asyncio` + **httpx AsyncClient** + **testcontainers** (real Postgres) + `factory_boy` | §13. |
| Errors / monitoring | **Sentry** + **structlog** (JSON logs) + **Prometheus/Grafana** + OpenTelemetry traces | §14. |
| Containers / CI | **Docker** (multi-stage) + **GitHub Actions** | §15. |
| Hosting | Start: one VPS (Hetzner/OVH) with Docker Compose + managed Postgres backups. Scale: split app/worker/DB nodes or move to managed (Fly.io, Render, AWS) | §16. |

---

## 4. Multi-Tenancy Design (the most important architectural decision)

**Model: shared database, shared schema, `tenant_id` column on every tenant-owned table, enforced by Postgres Row-Level Security (RLS).**

Rejected alternatives: schema-per-tenant (migration hell at 50+ tenants), database-per-tenant (ops hell, kills cross-tenant analytics). Shared-schema + RLS is what most B2B SaaS at your scale runs.

### 4.1 Tenant resolution (per request)

Each agency site runs on its own domain (`agencyx.com`) or a subdomain of yours (`agencyx.yourplatform.com`). A middleware resolves the tenant **before anything else**:

```
Host header → lookup domain in tenant_domains (cached in Redis, TTL 5 min)
  → found:  request.state.tenant = tenant  (id, plan, status, settings)
  → not found / tenant suspended: 404 / 402
```

Admin/platform routes (`admin.yourplatform.com`) bypass tenant scoping but require platform-staff roles.

### 4.2 Three layers of isolation (defense in depth)

1. **Middleware** sets `request.state.tenant_id`; a `TenantDep` dependency injects it into every route.
2. **Repository layer**: every query goes through repositories that require `tenant_id` — there is no "get by id" without it.
3. **Postgres RLS** as the safety net: on each request the session runs `SET LOCAL app.tenant_id = '<uuid>'`, and every tenant table carries a policy:

```sql
ALTER TABLE listings ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON listings
  USING (tenant_id = current_setting('app.tenant_id')::uuid);
```

Even if a developer writes a buggy query, Postgres refuses to return another tenant's rows. **This single decision is what lets you sleep at night.** (Connect as a non-superuser role — RLS doesn't apply to table owners/superusers.)

### 4.3 What is tenant-scoped vs global

- **Tenant-scoped:** users*, agents, teams, listings, media, leads, pipelines, messages, appointments, content pages, blog posts, reviews, notifications, analytics events, documents, saved searches.
- **Global (platform-level):** tenants, tenant_domains, plans/subscriptions, platform staff, feature flags, currency/i18n reference data.
- *A user account belongs to a tenant (a buyer registers on *an agency's site*). If you later want cross-agency accounts, add a global identity table and per-tenant memberships — don't build that now.

### 4.4 Per-tenant configuration

`tenants.settings` (JSONB, validated by a Pydantic schema) holds: branding (logo, colors), locales enabled (ar/fr/en), currency (DZD/EUR), contact channels, feature toggles (blog on/off, valuations on/off), lead-routing mode, email sender identity, custom SEO defaults. The frontend fetches `GET /api/v1/site/config` to render the right brand.

### 4.5 Tenant-aware everything else

- **Object storage keys:** `tenants/{tenant_id}/listings/{listing_id}/{photo_id}.webp` — enables per-tenant export/wipe.
- **Rate limits:** keyed per tenant *and* per IP, with plan-based quotas.
- **Search indexes:** Meilisearch index per tenant (or one index filtered by tenant_id — start with the filter).
- **Emails:** sent from per-tenant identities (`contact@agencyx.com`) with your platform as fallback; SPF/DKIM per domain.
- **Data export & deletion:** must be per-tenant from day one (offboarding + privacy law compliance, §10.12).

---

## 5. Project Structure (modular monolith)

Feature-first ("vertical slice") layout. Every module owns its models, schemas, service, and routes. A new dev finds *everything about leads* in one folder.

```
backend/
├── pyproject.toml               # deps via uv; ruff + mypy config
├── alembic/                     # migrations (one head, reviewed by hand)
├── docker/                      # Dockerfile, compose files, entrypoints
├── scripts/                     # seed_demo_data.py, create_tenant.py, ...
├── tests/                       # mirrors app/ structure
└── app/
    ├── main.py                  # app factory, middleware, router mounting, lifespan
    ├── core/                    # cross-cutting concerns — no business logic
    │   ├── config.py            #   pydantic-settings; fail fast on missing env
    │   ├── database.py          #   async engine, session factory, RLS session setup
    │   ├── security.py          #   JWT encode/decode, Argon2 hashing, token rotation
    │   ├── permissions.py       #   RBAC: roles, permission constants, require() deps
    │   ├── tenancy.py           #   tenant middleware + TenantDep
    │   ├── exceptions.py        #   AppError hierarchy → RFC 9457 problem+json handler
    │   ├── pagination.py        #   cursor pagination helpers
    │   ├── rate_limit.py        #   Redis sliding-window limiter dependency
    │   ├── events.py            #   in-process domain event bus (lead.created, ...)
    │   ├── logging.py           #   structlog config, request-id middleware
    │   └── i18n.py              #   locale negotiation, translated-field helpers
    ├── common/                  # shared value objects: Money, Address, GeoPoint, enums
    ├── integrations/            # thin adapters, one per external service
    │   ├── email/               #   EmailService interface + Brevo/SES impls
    │   ├── sms/                 #   SMS/WhatsApp
    │   ├── storage/             #   S3 presign/put/delete
    │   ├── geocoding/
    │   ├── payments/            #   Stripe / Chargily behind BillingProvider
    │   ├── esignature/
    │   └── portals/             #   syndication adapters (one class per portal)
    ├── workers/                 # celery_app.py, beat schedule, task modules
    └── modules/
        ├── tenants/             # tenant CRUD, domains, settings, plans, billing
        ├── auth/                # register, login, refresh, MFA, password reset, sessions
        ├── users/               # profiles, roles, account settings, GDPR export/delete
        ├── agents/              # agent profiles, teams, territories, performance stats
        ├── listings/            # properties, statuses, features, publishing workflow
        ├── media/               # upload sessions, image pipeline, galleries, virtual tours
        ├── search/              # search endpoints, filters, geo queries, index sync
        ├── leads/               # lead capture, CRM pipeline, assignment, activities, notes
        ├── clients/             # contact records, communication history, tags
        ├── messaging/           # conversations, WebSocket, unread counts
        ├── appointments/        # tour booking, agent availability, calendar sync
        ├── valuations/          # home-valuation requests + estimation logic
        ├── favorites/           # saved listings, saved searches, alert preferences
        ├── content/             # CMS: pages, blog, neighborhoods, market reports, media
        ├── reviews/             # testimonials with moderation
        ├── notifications/       # in-app + email/SMS fan-out, user preferences
        ├── transactions/        # deals, milestones, documents, commissions
        ├── analytics/           # event ingestion, dashboards, report queries
        ├── compliance/          # legal pages versions, consent records, audit access
        └── webhooks/            # outbound webhooks + inbound (payments, portals)
```

**Inside every module** — the same four files (plus extras as needed):

```
modules/listings/
├── models.py        # SQLAlchemy models (table = truth)
├── schemas.py       # Pydantic: ListingCreate / ListingUpdate / ListingOut / filters
├── repository.py    # all DB queries for this module (tenant_id required everywhere)
├── service.py       # business logic; the ONLY layer routes may call
├── router.py        # thin HTTP layer: parse → call service → return schema
├── permissions.py   # module-specific permission checks
└── events.py        # domain events this module emits/handles
```

**The golden rules every dev must follow**

1. Routers never touch the DB. Routers call services; services call repositories.
2. Modules never import another module's `models.py` or `repository.py` — they call the other module's **service** or subscribe to its **events**. (This is what keeps the monolith modular.)
3. Every repository method takes `tenant_id`. No exceptions.
4. Every schema that leaves the API is an explicit `*Out` model — never return ORM objects or `dict`s (prevents accidental field leaks like password hashes or internal notes).
5. Anything > ~200ms of work goes to Celery.

---

## 6. Data Model — Core Tables

Naming: snake_case, plural tables, `id UUID PK (uuid7)`, `tenant_id UUID` on tenant tables, `created_at/updated_at timestamptz` everywhere, soft delete (`deleted_at`) only where business history matters (listings, leads, users).

### 6.1 Identity & tenancy

```
tenants           id, name, slug, status(active|trial|suspended), plan_id,
                  settings JSONB, created_at
tenant_domains    id, tenant_id, domain UNIQUE, is_primary, ssl_status, verified_at
plans             id, name, price, currency, limits JSONB (max_listings, max_agents, ...)
subscriptions     id, tenant_id, plan_id, status, provider, provider_ref,
                  current_period_end
users             id, tenant_id, email (UNIQUE per tenant), password_hash,
                  role(visitor|buyer|seller|agent|team_lead|admin|marketing),
                  status, locale, phone, mfa_secret, last_login_at, deleted_at
sessions          id, user_id, refresh_token_hash, family_id, user_agent, ip,
                  expires_at, revoked_at            -- rotating refresh tokens (§7)
```

### 6.2 Agents & teams

```
agent_profiles    id, tenant_id, user_id FK, slug, bio (i18n JSONB), photo_key,
                  specialties[], service_areas geometry(MultiPolygon, 4326),
                  license_no, socials JSONB, is_published
teams             id, tenant_id, name, lead_user_id
team_members      team_id, user_id, role_in_team
```

### 6.3 Listings (the heart)

```
listings          id, tenant_id, reference_code (human: "AG-2026-00123"),
                  agent_id, status(draft|review|published|reserved|sold|rented|archived),
                  purpose(sale|rent|rent_daily), property_type(apartment|house|villa|
                    land|office|retail|warehouse|...),
                  title JSONB {ar,fr,en}, description JSONB,
                  price NUMERIC(14,2), currency, price_period(null|month|day),
                  negotiable BOOL,
                  beds SMALLINT, baths SMALLINT, area_built NUMERIC, area_land NUMERIC,
                  floor SMALLINT, floors_total SMALLINT, year_built SMALLINT,
                  features JSONB (["elevator","parking","garden",...]),
                  address JSONB, location geometry(Point, 4326),
                  neighborhood_id FK NULL,
                  search_vector tsvector (generated),      -- Postgres FTS
                  published_at, expires_at, view_count (denormalized),
                  created_by, deleted_at
listing_media     id, tenant_id, listing_id, kind(photo|video|tour_3d|floorplan|doc),
                  storage_key, variants JSONB ({thumb,card,full,webp...}),
                  position, alt_text JSONB, is_cover
listing_status_history  listing_id, from_status, to_status, changed_by, changed_at
listing_views     (analytics; see §8.15 — bucketed counts, not row-per-view forever)
```

Indexes that matter: `GIST(location)`, `GIN(search_vector)`, `GIN(features)`, `(tenant_id, status, published_at DESC)`, `(tenant_id, purpose, property_type, price)`.

### 6.4 Leads & CRM

```
leads             id, tenant_id, contact_id FK, listing_id NULL, agent_id NULL,
                  source(listing_form|valuation|search_signup|chat|phone|portal|ad),
                  source_meta JSONB (utm_*, page, campaign),
                  stage(new|contacted|qualified|touring|offer|won|lost),
                  score SMALLINT, lost_reason, first_response_at, created_at
contacts          id, tenant_id, first/last name, email, phone, whatsapp,
                  consent JSONB (marketing_email, sms, ts + proof), tags[], notes
lead_activities   id, tenant_id, lead_id, actor_id, type(note|call|email|sms|
                  status_change|assignment|tour|system), payload JSONB, created_at
pipelines / pipeline_stages   -- only if you make stages configurable per tenant;
                                 start with the fixed enum above
assignment_rules  id, tenant_id, strategy(round_robin|territory|listing_agent),
                  config JSONB
```

### 6.5 Engagement & communication

```
favorites         user_id, listing_id, created_at  (PK: user+listing)
saved_searches    id, tenant_id, user_id, name, filters JSONB, frequency(instant|
                  daily|weekly), last_run_at, is_active
conversations     id, tenant_id, subject_listing_id NULL, created_at
conversation_participants  conversation_id, user_id, last_read_at
messages          id, tenant_id, conversation_id, sender_id, body, attachments JSONB,
                  created_at   (immutable)
appointments      id, tenant_id, listing_id, agent_id, contact_id, type(visit|call|
                  video), status(requested|confirmed|done|cancelled|no_show),
                  starts_at, ends_at, notes
agent_availability id, agent_id, weekday, start_time, end_time  (+ exceptions table)
valuation_requests id, tenant_id, contact info, property JSONB, status,
                  estimate_low/high, assigned_agent_id
```

### 6.6 Content, reviews, notifications

```
pages             id, tenant_id, slug, type(page|landing), title/body JSONB (i18n),
                  seo JSONB, status(draft|published), published_at, author_id
posts             id, tenant_id, slug, title/excerpt/body JSONB, cover_key,
                  category_id, tags[], status, published_at, author_id
neighborhoods     id, tenant_id, slug, name JSONB, description JSONB,
                  boundary geometry(MultiPolygon) NULL, stats JSONB, media JSONB
market_reports    id, tenant_id, period, area, stats JSONB, gated BOOL, file_key
reviews           id, tenant_id, author_name, contact_id NULL, agent_id NULL,
                  rating SMALLINT, body, status(pending|approved|rejected), source
notifications     id, tenant_id, user_id, type, payload JSONB, read_at, created_at
notification_prefs user_id, channel(email|sms|push|inapp) × type matrix JSONB
```

### 6.7 Transactions & compliance

```
deals             id, tenant_id, listing_id, lead_id, buyer_contact_id,
                  seller_contact_id, agent_id, status(open|under_contract|closed|
                  fell_through), agreed_price, key_dates JSONB
deal_milestones   id, deal_id, name, due_at, completed_at, owner_id
documents         id, tenant_id, deal_id NULL, listing_id NULL, kind, storage_key,
                  uploaded_by, esign_status, sha256, created_at
commissions       id, deal_id, agent_id, basis, rate, amount, status(pending|paid)
audit_log         id, tenant_id, actor_id, action, entity_type, entity_id,
                  before/after JSONB (redacted), ip, user_agent, created_at
                  -- append-only; INSERT-only role; §10.11
consent_records   id, tenant_id, contact_id/user_id, purpose, granted BOOL,
                  proof JSONB, created_at
```

---

## 7. Authentication & Authorization

### 7.1 Authentication flow

- **Password auth:** Argon2id hashes (memory 64MB, iterations tuned to ~50ms). Generic error messages ("invalid credentials" — never "user not found").
- **Tokens:** short-lived **access JWT (15 min)** returned to the client + **refresh token (30 days)** in an `httpOnly; Secure; SameSite=Lax` cookie, stored **hashed** in `sessions`.
- **Rotation with reuse detection:** every refresh issues a new refresh token in the same `family_id` and revokes the old one. If a *revoked* token is presented (theft indicator), revoke the whole family and force re-login. This is the modern standard.
- **JWT claims:** `sub` (user id), `tid` (tenant id), `role`, `jti`, `exp`. Verify `tid` matches the request's resolved tenant on **every** authenticated request — a token from agency A must be useless on agency B's domain.
- **Logout / revoke-all-sessions:** delete session rows + short-TTL Redis denylist of `jti`s.
- **Email verification** required before an account can message agents or book tours. **Password reset** via single-use, 30-min, hashed tokens.
- **MFA (TOTP)** mandatory-optional: offered to all, *enforced* for admin and agent roles.
- **OAuth (Google/Facebook sign-in)** for buyers — reduces friction on lead-heavy pages. Via `authlib`.
- **Account lockout:** exponential backoff after 5 failed attempts (per account + per IP, Redis).

### 7.2 Authorization: RBAC + ownership + object-level checks

Roles (from the product spec): `visitor` (no account), `buyer_renter`, `seller`, `agent`, `team_lead`, `admin`, `marketing` + platform-level `platform_admin`, `platform_support`.

Implementation:

- A static **permission matrix** in `core/permissions.py`: `Permission.LISTING_PUBLISH`, `Permission.LEAD_VIEW_ALL`, etc. Roles map to permission sets. Stored in code, not DB (auditable in git, testable).
- FastAPI dependency: `user = Depends(require(Permission.LISTING_PUBLISH))`.
- **Ownership scoping** on top of RBAC: an `agent` sees *their* leads/listings; `team_lead` sees the team's; `admin` sees the tenant's. Implemented in repositories as scope filters (`for_actor(user)`), not in routers.
- **IDOR defense:** every "get by id" query includes tenant_id + actor scope. Tests in §13 assert cross-tenant and cross-role access fails.

---

## 8. Feature Modules — Every Feature, A to Z

Each module below maps 1:1 to the agency feature map (public site, lead gen, portals, back-office).

### 8.1 Listings (`modules/listings`)

The core inventory. Since there's no MLS in your market, the workflow is authoring-first:

- **Publishing workflow:** `draft → review → published → reserved → sold/rented → archived`, with `listing_status_history` for every transition. Configurable per tenant whether agents can self-publish or need admin review.
- **Human reference codes** (`AG-2026-00123`) per tenant — agencies live by these on the phone.
- **i18n fields:** title/description as JSONB `{ar, fr, en}`; API returns the negotiated locale with fallback chain (e.g., ar → fr → en).
- **Features as JSONB array** validated against a controlled vocabulary (so filters stay consistent) + GIN index for "has elevator AND parking" queries.
- **Geo:** address geocoded in a Celery task on save; `location` stored as PostGIS point; neighborhood auto-assigned by point-in-polygon lookup.
- **Expiry:** optional `expires_at` with a Beat job that flags stale listings for agent review (stale inventory kills trust).
- **Similar listings:** same purpose + type, price ±20%, within 5 km, ordered by recency — a single indexed PostGIS query. Upgrade to behavioral similarity later.
- **Endpoints (public):** `GET /listings` (search — §8.3), `GET /listings/{slug-or-ref}`; **(portal):** full CRUD, status transitions, media ordering, `POST /listings/{id}/duplicate`.
- **Domain events:** `listing.published` triggers → index in search, match saved-search alerts, syndicate to portals, notify the assigned agent.

### 8.2 Media pipeline (`modules/media`)

Photos sell property; this module must be excellent.

1. **Direct-to-storage uploads:** client asks `POST /media/uploads` → API validates (type, size ≤ 25MB, count quota per plan) → returns a **presigned PUT URL**. The file never passes through FastAPI — no worker starvation from big uploads.
2. Client confirms → Celery task: verify **magic bytes** (not just extension), strip EXIF GPS (privacy!), generate variants with libvips — `thumb 320w`, `card 640w`, `gallery 1280w`, `full 1920w`, all WebP + JPEG fallback — plus a **blurhash** placeholder string for the frontend.
3. Optional per-tenant **watermarking** (agencies love this; it also deters listing theft by competitors).
4. Serve via CDN with immutable cache headers (content-hashed keys).
5. Video: store MP4s up to a plan-limit, or just store YouTube/Vimeo/Matterport embed URLs (`kind=tour_3d`) — don't build video transcoding in v1.
6. Documents (floor plans, brochures, contracts): private bucket, **presigned GET (15 min)** only after permission check — never public.

### 8.3 Search & discovery (`modules/search`)

The most-used feature on the public site.

- **Filters:** purpose, type, price min/max, beds/baths min, areas, features[], neighborhood, keyword. All parsed into a single Pydantic `ListingSearchParams` — validation prevents nonsense (`price_min > price_max` → 422).
- **Keyword search:** Postgres FTS on the generated `search_vector` (title + description + neighborhood, per-locale configs). Move to Meilisearch when relevance or scale demands (sync via `listing.published/updated/unpublished` events; nightly reconciliation job).
- **Geo search for split map+list view:**
  - `in_bbox=minLon,minLat,maxLon,maxLat` (map viewport)
  - `near=lon,lat&radius_km=5`
  - `in_polygon=` (drawn area / neighborhood boundary)
  - All via PostGIS `ST_Intersects`/`ST_DWithin` on the GIST index.
- **Map pins endpoint:** `GET /listings/map` returns only `{id, lat, lng, price, status}` — lightweight; **server-side clustering** (`ST_ClusterKMeans` or geohash buckets) when a viewport has > 500 hits.
- **Sorting:** newest, price ↑↓, area — plus a `featured` boost (paid placement is an agency upsell).
- **Pagination:** cursor-based (keyset) — stable under insertions, fast at any depth (§9).
- **SEO support endpoints:** `sitemap.xml` per tenant (listings + pages + posts), canonical slugs, `GET /listings/{id}` includes JSON-LD-ready structured data (`RealEstateListing`) for the frontend to embed.

### 8.4 Leads & CRM (`modules/leads`, `modules/clients`)

Where the money is. Every public form funnels here.

- **Capture endpoints** (public, heavily rate-limited + spam-protected §10.8): listing contact form, tour request, valuation request, saved-search signup, guide download, chat handoff, generic contact. Each creates/merges a `contact` (dedupe by email/phone per tenant) + a `lead` with full `source_meta` (UTM params, page URL, referrer) — **lead-source tracking is built in from day one**, it's what agencies judge marketing spend by.
- **Assignment engine:** strategy per tenant — `listing_agent` (default: lead goes to the listing's agent), `round_robin` (with per-agent caps and online/away status), `territory` (point-in-polygon against `agent_profiles.service_areas`). Unassigned leads escalate to admin after N minutes.
- **Speed-to-lead:** on `lead.created` → instant notifications to the assigned agent (push/in-app + email + optional WhatsApp/SMS). Track `first_response_at`; show response-time stats on dashboards (response speed is the #1 conversion factor and agencies know it).
- **Pipeline:** `new → contacted → qualified → touring → offer → won|lost` with drag-drop-friendly `PATCH /leads/{id}/stage`, required `lost_reason`, and a full `lead_activities` timeline (calls, notes, emails, tours, automatic system events).
- **Follow-up sequences (drip):** per-tenant email/SMS sequences (e.g., day 0 / 2 / 7) executed by Beat; stop automatically on reply or stage change. Start with fixed templates; visual builder later.
- **Lead scoring v1:** simple rules (source quality, budget match, activity recency) — a `score` you can later replace with a model without touching the API.
- **Contact timeline:** one page per contact aggregating every lead, message, tour, and note — the "CRM view" agents actually use.

### 8.5 Agents & teams (`modules/agents`)

- Public **agent directory** + **profile pages** (bio, specialties, service areas, active/sold listings, reviews, contact/booking CTA) — SEO-relevant, slug-based.
- **Performance stats** per agent/team: active listings, leads by stage, response time, tours, deals closed, commission totals (from §8.13) — powering the agent dashboard and the team-lead rollup view.
- Team leads get scoped visibility over team members' pipelines (via the `for_actor` scoping in §7.2).

### 8.6 Messaging (`modules/messaging`)

- **Conversations** between client and assigned agent, optionally anchored to a listing.
- **WebSocket** endpoint `/ws` (authenticated via short-lived ticket obtained over HTTPS — don't put long-lived JWTs in query strings). Redis pub/sub fans out across app replicas. Fallback: REST polling endpoint for degraded clients.
- Messages are immutable; attachments go through the media pipeline (private bucket).
- Unread counts per participant (`last_read_at`); offline recipients get an email digest after 15 min ("You have a message about Villa X").
- **Live-chat widget leads:** anonymous visitor chats create a lightweight session; the moment contact info appears, it converts into a contact + lead (source=`chat`).

### 8.7 Appointments & tours (`modules/appointments`)

- Agent availability (weekly template + exceptions), tenant-level buffer rules.
- Public `GET /agents/{id}/slots?date=` computes free slots (availability − existing appointments); booking creates `requested` → agent confirms.
- Reminders via Beat: 24h and 1h before (email/SMS/WhatsApp). No-show tracking feeds lead score.
- **iCal feed** per agent (secret URL) for calendar apps; two-way Google Calendar sync is a v2 feature — don't block launch on it.

### 8.8 Valuations (`modules/valuations`) — the seller lead magnet

- Multi-step form (address → property details → contact) matching the mobile-first spec; each step validated separately so partial abandons still capture what was given.
- v1 estimate: comparable published/sold listings in radius (PostGIS) → price/m² band → `estimate_low/high`. Present as a *range* with disclaimers; route to an agent for the "real" valuation — the goal is the conversation, not algorithmic precision.
- Creates a lead (source=`valuation`) with property payload attached.
- **Mortgage / affordability calculator:** a stateless `POST /tools/mortgage-estimate` endpoint (price, down payment, rate, term → monthly payment, Decimal math) with per-tenant default rates in settings; the listing detail page calls it pre-filled with the listing price. Optional "email me this estimate" turns it into one more lead source.

### 8.9 Favorites, saved searches & alerts (`modules/favorites`)

- Favorites: idempotent PUT/DELETE, powers the buyer dashboard.
- **Saved searches** store the same `ListingSearchParams` JSON as §8.3. The alert matcher runs on `listing.published` (instant) and via Beat (daily/weekly digests): match new listings against saved filters → notification fan-out. This is the single stickiest retention feature a portal has — build it early.
- Anonymous visitors can create a saved search with just an email (double-opt-in) → becomes a lead (source=`search_signup`).

### 8.10 Content / CMS (`modules/content`)

Powers: home page blocks, buyers/sellers pages, about, neighborhood guides, market reports, blog, landing pages, legal pages.

- Structured **pages** (slug, i18n blocks, SEO meta, draft/published + preview tokens) — enough CMS for an agency site; don't build a page-builder, the frontend defines block types and the backend stores validated block JSON.
- **Blog** with categories/tags, scheduled publishing (Beat), RSS.
- **Neighborhood guides** with optional PostGIS boundary → auto-links to listings inside the polygon and live stats (count, median price) computed nightly.
- **Market reports:** stats JSONB + generated PDF (worker) — **gated**: email required to download → lead.
- **Legal pages are versioned** (privacy, terms, fair-treatment/anti-discrimination statement, license disclosures) — you must be able to prove what a user consented to and when (§10.12).

### 8.11 Reviews (`modules/reviews`)

Submission (verified clients or moderated public), `pending → approved|rejected` moderation queue, aggregation per agent and per tenant, embeddable on profiles/home. Optionally import Google reviews read-only later.

### 8.12 Notifications (`modules/notifications`)

One internal API every module calls: `notify(user, type, payload)` →

1. Look up user channel preferences (per-type matrix).
2. In-app row (+ WebSocket push if online).
3. Email/SMS/WhatsApp via integration adapters, rendered from versioned templates (MJML → HTML) in the user's locale.
4. All sends recorded with provider message-id + delivery status webhooks — deliverability is debuggable.
5. Quiet hours + digest batching to avoid spamming agents at 3am.

### 8.13 Transactions & deals (`modules/transactions`)

Back-office deal tracking once a lead converts: deal record linked to listing/lead/contacts, **milestones** with due dates and owners (reminders via Beat), **documents** vault (private storage, sha256, e-signature adapter), **commission tracking** (rate/basis/amount per agent, marks paid). Keep v1 simple — checklist + docs + commissions — it already beats spreadsheets.

### 8.14 Portal syndication (`integrations/portals` + webhooks)

Your market's replacement for MLS — *export* instead of import:

- One adapter class per target portal (e.g., local property sites / Facebook catalog / future partners): `push(listing) / update / remove`, mapping your schema to theirs.
- Fired from `listing.published/updated/archived` events; per-portal sync-state table (last_pushed_at, remote_id, last_error) with retries + exponential backoff.
- Also generate **XML/CSV feeds** at stable per-tenant URLs (many portals pull rather than push).

### 8.15 Analytics & reporting (`modules/analytics`)

- **Event ingestion:** `POST /events` (batched, anonymous-ok): listing_view, search, favorite, form_start/submit, page_view. Written to an append-only `analytics_events` table (monthly partitions), aggregated nightly into rollup tables (`listing_stats_daily`, `lead_funnel_daily`, `source_performance_daily`). Raw events pruned after 90 days.
- **Dashboards served from rollups, never raw events:** traffic & top listings, lead volume/source/conversion, agent performance, seller-dashboard stats (views/saves/inquiries per listing — a headline feature for seller retention).
- Keep Google Analytics on the frontend too — your internal analytics answer *business* questions GA can't (which listings/campaigns produce leads that close).

### 8.16 Tenant administration & billing (`modules/tenants`)

Platform-level (your business):

- Tenant lifecycle: create (with seed data), trial, activate, suspend (data kept, site shows maintenance), offboard (**full export** then scheduled deletion).
- Domain management: add domain, DNS verification (TXT), automatic TLS (Caddy on-demand certs or Cloudflare for SaaS).
- Plans & quotas enforced in code (`max_listings`, `max_agents`, storage GB, monthly emails): checked at write-time, surfaced in `GET /site/config`.
- Billing: subscription lifecycle via `BillingProvider` (Stripe/Chargily), inbound payment webhooks (signature-verified §10.9), dunning (grace period → suspend).
- Platform admin endpoints: cross-tenant metrics, impersonation ("login as tenant admin" — **audit-logged**, time-boxed, visible banner).

### 8.17 Compliance module (`modules/compliance`)

Consent records (what/when/proof), cookie-consent config per tenant, data-subject request handling (export/delete §10.12), legal page versioning (§8.10), audit access reports. Small module, big trust-builder in a market where privacy laws (e.g., Algeria's 18-07, GDPR for EU tenants) apply.

### 8.18 AI features (design for now, ship later)

The API surface stays stable while implementations improve:

- `POST /listings/{id}/generate-description` — LLM drafts the i18n description from structured fields (agent edits before save).
- Lead-scoring model replacing the rules score (§8.4).
- AI chat assistant answering from listings + neighborhood content (RAG) with lead handoff.
- Behavioral recommendations ("similar to what you saved") from analytics events.

Isolate all of it behind `integrations/ai/` — provider-agnostic.

---

## 9. API Design Conventions (contract every dev follows)

- **Versioned base path:** `/api/v1/...`. Version in the URL, breaking changes → `/v2` (rare if you're additive).
- **Resource naming:** plural nouns (`/listings/{id}/media`), verbs only for true actions (`/listings/{id}/publish`).
- **Errors — RFC 9457 `application/problem+json`** from one global exception handler:

```json
{
  "type": "https://api.example.com/errors/quota-exceeded",
  "title": "Listing quota exceeded",
  "status": 403,
  "detail": "Plan 'Starter' allows 50 published listings.",
  "instance": "/api/v1/listings",
  "request_id": "..."
}
```

  Internal exceptions never leak (no stack traces, no SQL). `request_id` in every response header + error body for support.
- **Pagination:** cursor/keyset by default — `?cursor=...&limit=24` → `{items, next_cursor, total_estimate}`. Offset pagination only in the admin panel where page numbers matter.
- **Filtering/sorting:** explicit whitelisted params via Pydantic models — never pass raw query strings to SQL.
- **Idempotency:** `Idempotency-Key` header supported on POSTs that create money-adjacent or duplicate-sensitive things (leads, bookings, payments); key + response cached in Redis for 24h.
- **Consistency rules:** camelCase JSON (single `alias_generator`), UTC ISO-8601 timestamps, money as `{"amount": "25000000.00", "currency": "DZD"}` (string amount — never floats), enums as lowercase strings.
- **Partial updates:** `PATCH` with Pydantic `exclude_unset=True` semantics.
- **Bulk endpoints** where the UI needs them (reorder media, bulk lead assignment) — 20 requests in a loop is not a UI strategy.
- **Deprecation policy:** `Deprecation` + `Sunset` headers, minimum 90 days notice, documented in the changelog.

---

## 10. Security — A to Z

Ordered from the request edge inward. Treat this section as a production gate: nothing ships until every item is either done or consciously waived in writing.

### 10.1 Edge & transport
- TLS 1.2+ only, HSTS (`max-age=31536000; includeSubDomains`), automatic certs (Caddy / Cloudflare for SaaS for tenant custom domains).
- WAF + DDoS protection at the CDN (Cloudflare free tier already blocks a lot).
- Security headers on every response (middleware): `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, restrictive `Permissions-Policy`, and CSP for any HTML the API serves (docs pages).
- **CORS:** exact-match allowlist built from `tenant_domains` — never `*` with credentials.

### 10.2 Rate limiting & abuse (Redis sliding window)
- Layered budgets: per-IP global (e.g., 100/min), per-endpoint (login 5/min/IP, public lead forms 3/min/IP, search 30/min), per-user, per-tenant plan quota.
- Return `429` + `Retry-After`. Log limit-hits — they're your early-warning system for scraping and credential stuffing.
- **Anti-scraping** (listings data is valuable): stricter anonymous budgets, monitor sequential-ID walking (another reason UUIDs), optional Turnstile challenge on suspicion.

### 10.3 Authentication hardening
(§7 covers design.) Additionally: Argon2id parameters reviewed yearly; breached-password check on registration (k-anonymity HaveIBeenPwned API); session list visible to users ("log out other devices"); admin/agent MFA enforced; email-change requires re-auth + confirmation to both old and new address.

### 10.4 Input validation & injection
- **Pydantic at every boundary** — body, query, headers, webhooks. Unknown fields rejected (`extra="forbid"` on input schemas).
- **SQL injection:** SQLAlchemy parameterized queries only; raw SQL requires review and bound params. No string-formatted SQL, ever.
- **XSS:** API returns JSON, but any rich text (listing descriptions, blog posts) is sanitized server-side (`nh3`/ammonia allowlist) at **write** time — never trust "the frontend will escape it".
- **SSRF:** any user-supplied URL you fetch (tour embeds, webhook targets) → validate scheme/host, resolve DNS, block private IP ranges, no redirects-to-private.
- Upload validation: magic bytes, size, image re-encode (a re-encoded image can't carry payloads), filenames never used as storage keys.

### 10.5 Authorization (recap as security control)
RBAC + ownership scoping + RLS (§4.2, §7.2). The **cross-tenant test suite** (§13) is a security control: for every resource type, assert tenant B's admin gets 404 on tenant A's objects.

### 10.6 Secrets & configuration
- No secrets in git — `pydantic-settings` from env; production secrets in a manager (Doppler/Infisical/SOPS/AWS SM), injected at deploy.
- Separate credentials per environment; DB app-user has least privilege (no DDL in prod; migrations run with a separate role).
- Key rotation runbook: JWT signing keys support `kid` rotation; storage and provider keys rotated on staff departure.

### 10.7 Data protection
- Encryption at rest (disk-level for PG + bucket encryption) and in transit everywhere (app↔DB `sslmode=require` if networked).
- Field-level encryption for high-sensitivity values (MFA secrets, provider tokens) using `cryptography` (Fernet/AES-GCM) with keys outside the DB.
- PII minimization in logs: structlog processor that redacts email/phone/tokens; never log request bodies of auth or lead endpoints.
- Backups encrypted; restore access limited and audited.

### 10.8 Form spam & bot defense (public lead forms are magnets)
Honeypot field + minimum-fill-time check + per-IP rate limit as baseline; Cloudflare Turnstile (privacy-friendlier than reCAPTCHA) when heat rises; disposable-email domain blocklist; double-opt-in for saved-search subscriptions.

### 10.9 Webhooks & third-party callbacks
- Inbound (payments, e-signature, email delivery): verify HMAC signatures, enforce timestamp freshness (±5 min), idempotent processing by event id, dedicated `webhooks` module.
- Outbound (to tenant systems): signed payloads (HMAC-SHA256 header), retries with backoff, per-endpoint failure circuit breaker.

### 10.10 Dependency & supply chain
`uv.lock` committed; Dependabot/Renovate weekly; `pip-audit` + `ruff` + `bandit` in CI; Docker base images pinned by digest and rebuilt weekly; no dependency added without a maintainer/health check.

### 10.11 Audit & detection
- Append-only `audit_log` (INSERT-only DB role) for: auth events, permission changes, listing publish/price changes, lead reassignment, data exports, impersonation, deletions.
- Alerting rules (Grafana/Sentry): login-failure spikes, 403/404 bursts per token (IDOR probing), refresh-token-reuse events, unusual export volume.
- An incident-response one-pager: how to revoke all sessions, rotate keys, suspend a tenant, restore from backup. Write it before you need it.

### 10.12 Privacy & legal compliance
- Applies to EU tenants (GDPR) and Algeria (Law 18-07 — consent, purpose limitation, national data-handling rules): consent records with proof, purpose-tagged processing, DSR endpoints — `GET /me/export` (JSON+files) and `DELETE /me` (soft-delete → 30-day purge job, cascading through messages/leads with anonymization where business records must persist).
- Data-retention policy encoded as Beat jobs (e.g., anonymize lost leads after 24 months, prune raw analytics at 90 days).
- Cookie/tracking consent honored server-side (no analytics event ingestion for non-consenting sessions).
- Per-tenant Data Processing Agreement template — you are the *processor*, agencies are *controllers*. Have a lawyer review; this doc is not legal advice.

---

## 11. Performance & Caching

- **Latency budgets:** search p95 < 300ms, detail p95 < 150ms, writes p95 < 250ms. Measure from day one (§14) — budgets you don't measure are wishes.
- **Redis cache** (cache-aside, versioned keys `cache:{tenant}:{entity}:{id}:{v}`): site config (5 min), rendered page/nav content (5 min), search facet counts (60s), map clusters per viewport-hash (60s). Invalidate by bumping the entity version on write — no fragile key hunting.
- **HTTP caching:** `ETag`/`Last-Modified` on public GETs; `Cache-Control: public, s-maxage=60` lets the CDN absorb anonymous listing traffic (most of your traffic).
- **DB discipline:** `selectinload`/`joinedload` to kill N+1s (assert query counts in tests); covering indexes for the hot search paths (§6.3); `EXPLAIN ANALYZE` on any query touching listings before it ships; PgBouncer (transaction mode) once connections × replicas grow.
- **Payload discipline:** list endpoints return card-sized schemas, not full objects; map endpoint returns pins only; gzip/brotli at the proxy.
- **Load test before launch** (Locust/k6): the search page, a lead-form burst, and the WebSocket fan-out.

## 12. Background Jobs (Celery)

- **Queues by profile:** `default` (emails, notifications), `media` (CPU-heavy image work, few workers, memory-capped), `sync` (portals, geocoding), `analytics` (rollups). One slow queue can't starve lead notifications.
- **Beat schedule:** alert digests (daily/weekly), drip steps, appointment reminders, listing-expiry checks, nightly rollups, search-index reconciliation, retention/purge jobs, DB backup verification ping.
- **Rules:** every task idempotent (safe to retry), explicit `max_retries` + exponential backoff + jitter, task args are IDs not objects, dead-letter handling with Sentry alert, Flower (internal-only) for visibility.
- **Outbox pattern** for critical events: `lead.created` side-effects are enqueued in the same DB transaction as the lead row (an `outbox` table drained by a worker) — a Redis hiccup can't lose a lead notification. This is the one piece of "extra" architecture that pays for itself immediately.

## 13. Testing Strategy

- **Pyramid:** many service/repository tests (real Postgres via **testcontainers** — SQLite lies about JSONB/PostGIS/RLS), a solid layer of API tests (httpx `AsyncClient` against the app), few end-to-end smoke flows.
- **Non-negotiable suites:**
  - **Tenant isolation:** for every module, tenant B admin → tenant A resource = 404. Parametrized, automatic for new modules.
  - **RBAC matrix:** role × endpoint expected-status table-driven tests.
  - **Auth flows:** refresh rotation, reuse detection, lockout, reset-token single-use.
  - **Lead pipeline:** form → contact dedupe → assignment → notification outbox.
  - **Search correctness:** filters, geo bbox/radius, cursor stability.
  - **Money:** commission math with Decimal — property-based tests (hypothesis).
- Factories (`factory_boy`) for all models; deterministic seeds; migrations tested by upgrading from N-1 in CI; coverage gate ~85% on `modules/` and `core/` (chase meaningful tests, not the number).

## 14. Observability

- **Structured JSON logs** (structlog): every line carries `request_id`, `tenant_id`, `user_id`, route, duration. PII redacted (§10.7). Ship to Loki/CloudWatch.
- **Sentry** for exceptions (API + workers) with release tagging → errors map to deploys.
- **Metrics** (Prometheus + Grafana): request rate/latency/error by route & tenant, DB pool saturation, Celery queue depth & task latency, cache hit ratio, WebSocket connections — plus **business metrics**: leads/hour, notification delivery rate, search volume. A leads/hour flatline finds broken forms faster than any error log.
- **Tracing** (OpenTelemetry, FastAPI + SQLAlchemy + Celery instrumentation) once you have real traffic — finds the slow query inside the slow request.
- **Uptime & alerting:** external checks (health endpoint + a real search request) per region; alert on error-rate spikes, p95 breaches, queue-depth growth, disk >80%, cert expiry. `/healthz` (liveness) and `/readyz` (DB+Redis checks) endpoints.

## 15. Environments, CI/CD & Infrastructure

- **Environments:** local (Docker Compose: PG+PostGIS, Redis, MinIO, Mailpit) → staging (prod-shaped, anonymized seed data) → production. Config differs only by env vars.
- **CI (GitHub Actions), blocking:** ruff + mypy → pip-audit/bandit → full test suite (testcontainers) → migration upgrade test → Docker build (multi-stage, non-root user, pinned digests) → push image.
- **CD:** auto-deploy staging on main; production deploy is a manual approval of the same image (build once, promote). Zero-downtime: run Alembic migration (expand-contract pattern — additive first, code deploy, destructive change next release), rolling restart behind the proxy, `/readyz` gating.
- **Production topology v1 (one solid VPS is fine):** Caddy/Nginx → 2× app containers → 2× worker containers + beat → Postgres (same host or managed) → Redis. **Off-host, tested backups:** nightly `pg_dump` + WAL archiving (pgBackRest) to object storage, weekly automatic restore-verification job, 30-day retention. *An untested backup is a rumor.*
- **Runbooks in the repo:** deploy, rollback (previous image + down-migration policy), restore, tenant offboard, incident response.

## 16. Scaling Path (don't pre-build — know the order)

1. **0 → ~30 tenants:** the v1 topology above. Add PgBouncer early.
2. **Growing read traffic:** more app replicas + CDN caching (§11) — listings traffic is overwhelmingly anonymous reads; the CDN does the heavy lifting.
3. **Search pressure:** move FTS to Meilisearch (already event-synced, low-risk swap).
4. **DB pressure:** Postgres read replica for search/analytics reads; partition `analytics_events` (already monthly).
5. **Worker pressure:** scale queues independently (media workers separate node).
6. **Only then** consider extracting a service (media processing is the usual first candidate). The module boundaries make this a lift, not a rewrite.

## 17. Build Roadmap (phased, each phase shippable)

**Phase 0 — Foundations (weeks 1–3):** repo, CI, Docker Compose, core/ (config, DB, RLS session, errors, logging), tenancy middleware + tenants module, auth (register/login/refresh/reset/verify), users + RBAC, audit log skeleton. *Definition of done: two tenants on two domains, isolated, tested.*

**Phase 1 — Inventory & public site (weeks 3–7):** listings CRUD + workflow + i18n, media pipeline (presigned uploads, variants), search (filters + FTS + geo + map pins), agents module + public profiles, content module (pages, legal), site-config endpoint, sitemaps. *DoD: a complete public agency site can run on the API.*

**Phase 2 — Lead machine (weeks 7–11):** contacts + leads + capture endpoints + spam defense, assignment engine + outbox + notifications (email first), pipeline + activities, favorites + saved searches + instant alerts, valuations, appointments v1, buyer/seller dashboard endpoints. *DoD: a lead from any page reaches the right agent in under a minute, tracked end-to-end.*

**Phase 3 — Operate & retain (weeks 11–16):** messaging + WebSocket, drip sequences, reviews, blog + neighborhoods + market reports, analytics ingestion + rollups + dashboards, transactions/deals v1, portal syndication for the first local portal, billing + plan quotas. *DoD: an agency can run its whole operation in the product — and you can charge for it.*

**Phase 4 — Polish & scale:** MFA everywhere, Meilisearch, WhatsApp channel, calendar sync, AI description generation, lead scoring v2, load testing, pen test before big-agency onboarding.

## 18. Production-Readiness Checklist (print this)

**Security:** ☐ TLS+HSTS ☐ security headers ☐ CORS allowlist ☐ rate limits ☐ Argon2id ☐ refresh rotation + reuse detection ☐ MFA for staff ☐ RLS enabled + non-owner DB role ☐ cross-tenant tests green ☐ input sanitization (nh3) ☐ upload magic-byte + re-encode ☐ SSRF guards ☐ secrets manager ☐ webhook signatures ☐ audit log ☐ pip-audit/bandit in CI ☐ incident runbook
**Data:** ☐ nightly backups + WAL ☐ weekly restore test ☐ retention jobs ☐ DSR export/delete ☐ consent records ☐ PII-redacting logs
**Reliability:** ☐ /healthz + /readyz ☐ zero-downtime migrations ☐ outbox for lead events ☐ idempotent tasks ☐ dead-letter alerts ☐ uptime checks ☐ load test passed
**Quality:** ☐ ruff+mypy clean ☐ coverage gate ☐ RBAC matrix tests ☐ migration upgrade test in CI ☐ staging mirrors prod
**Business:** ☐ plan quotas enforced ☐ billing webhooks verified ☐ tenant offboard/export path ☐ per-domain email auth (SPF/DKIM/DMARC) ☐ sitemaps + structured data ☐ analytics rollups feeding dashboards

---

*Written as an opinionated blueprint: every choice here is replaceable, but each was picked to minimize ops burden for a small team shipping a multi-tenant product. Build Phase 0 exactly as specified — foundations are the one thing you can't cheaply redo.*
