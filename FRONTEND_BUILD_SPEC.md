# Frontend Build Specification — Multi-Tenant Real Estate Agency Platform

**Version 2.0 · 2026-09-03 · derived from the backend at commit `6df13f9` plus the uncommitted working tree**

This document is the build contract for the frontend. Every endpoint, field, enum value, status code and permission below was read from the running backend's generated API schema (182 paths, 240 operations) and cross-checked against the routers and services. Nothing is invented. Where the backend does not provide something a screen needs, the item is marked **GAP-nn** inline and collected in §6.

**Notation used throughout**

| Notation | Meaning |
|---|---|
| `POST /leads/capture` | Path relative to `/api/v1` unless it starts with `/healthz`, `/readyz`, `/internal` or `/docs` |
| `field?` | Optional on input / nullable on output |
| `Money` | A decimal **string** on output (e.g. `"150000.00"`); on input either a number or a numeric string with at most 2 decimals |
| `I18n` | `{ ar?: string, fr?: string, en?: string }` — a per-locale map (portal shapes) |
| `Page<T>` | `{ items: T[], nextCursor: string \| null, totalEstimate: number \| null }` |
| `Problem` | An RFC 9457 error body — see §2.6 |
| 🔒 `perm:name` | Route requires that RBAC permission |
| 👤 | Ownership-authorised: any signed-in tenant account, acting only on its own rows |
| 🌐 | Anonymous, tenant resolved from the request Host |
| 🛡 | Platform staff only, on the bare (non-tenant) host |

Query parameters are listed with their **exact wire spelling**. The backend is camelCase on the wire for JSON bodies and responses, but several list filters are snake_case (`agent_id`, `listing_id`, `start_from`, `start_to`, `category_id`, `agent_user_id`, `endpoint_id`, `tenant_id`). They are reproduced verbatim; do not "fix" them client-side.

---

## Table of contents

1. Overview
2. Global structure
3. Modules
   - 3.1 Site bootstrap & tenant state
   - 3.2 Authentication, session & account
   - 3.3 Public listings, search & map
   - 3.4 Public lead capture & WhatsApp handoff
   - 3.5 Public agents, reviews & tour booking
   - 3.6 Seller tools: valuation wizard & mortgage calculator
   - 3.7 Public content: pages, legal, guides, market reports, blog
   - 3.8 Anonymous saved-search alerts
   - 3.9 Cookie consent & analytics beacons
   - 3.10 Buyer account (`/me`)
   - 3.11 Portal shell & authorisation model
   - 3.12 Portal — Listings
   - 3.13 Portal — Media
   - 3.14 Portal — CRM (leads, contacts, activities, assignment)
   - 3.15 Portal — Agents & teams
   - 3.16 Portal — Tours & availability
   - 3.17 Portal — Deals, milestones, documents, commission
   - 3.18 Portal — Content CMS & blog
   - 3.19 Portal — Review moderation
   - 3.20 Portal — Analytics
   - 3.21 Portal — Users (tenant staff accounts)
   - 3.22 Portal — Syndication
   - 3.23 Portal — Webhooks
   - 3.24 Portal — Compliance
   - 3.25 Platform back-office
4. Cross-module flows
5. Build order
6. Gaps / Assumptions
7. Appendix A — Enums · Appendix B — Error catalogue · Appendix C — Rate limits · Appendix D — Tenant settings keys the backend reads

---

# 1. Overview

## 1.1 What the frontend is

One frontend codebase that renders **four distinct products** against one API. They differ in authentication model and in who the person is, not only in layout.

| Surface | Who | Host | Auth |
|---|---|---|---|
| **Public agency website** | Visitors: buyers, renters, sellers | The agency's own domain (e.g. `agency-a.com`, `demo.localhost`) | None; tenant resolved from Host |
| **Buyer account** (`/me`) | A visitor who registered (role `buyer_renter` or `seller`) | Same agency domain | Bearer token; ownership is the authorisation |
| **Agency portal** (`/portal`) | Agency staff: agent, team lead, marketing, admin | Same agency domain | Bearer token + RBAC permission + visibility scope |
| **Platform back-office** (`/platform`) | Operator staff: platform admin, platform support | The **bare** API host, not an agency domain | Separate bearer token + separate refresh cookie |

The public site, buyer account and portal all live on the agency domain because the backend resolves the tenant from the `Host` header of every request. A request to an unknown host gets a 404 on every route. The platform console lives on the bare host because its routes (`/api/v1/platform/*`) are tenant-exempt.

## 1.2 Roles

Eight roles exist. Six are tenant roles (one agency), two are platform roles (no agency).

| Role | Wire value | Where they live | Self-registers? |
|---|---|---|---|
| Buyer / renter | `buyer_renter` | Agency site → buyer account | Yes |
| Seller | `seller` | Agency site → buyer account (same capabilities as buyer today) | Yes |
| Agent | `agent` | Portal | No — created by an admin |
| Team lead | `team_lead` | Portal | No |
| Marketing | `marketing` | Portal | No |
| Admin | `admin` | Portal | No |
| Platform support | `platform_support` | Platform console | No — created by a platform admin |
| Platform admin | `platform_admin` | Platform console | No |

## 1.3 Permission matrix (authoritative, from the backend's static role → permission table)

| Permission | agent | team_lead | marketing | admin |
|---|:--:|:--:|:--:|:--:|
| `user:view` | – | – | – | ✅ |
| `user:manage` | – | – | – | ✅ |
| `listing:manage` | ✅ | ✅ | ✅ | ✅ |
| `listing:publish` | ⚠️ setting¹ | ✅ | – | ✅ |
| `lead:manage` | ✅ | ✅ | ✅ | ✅ |
| `lead:view_all` | – | ✅ | ✅ | ✅ |
| `lead:assign` | – | ✅ | ✅ | ✅ |
| `agent:manage` | – | ✅ | ✅ | ✅ |
| `appointment:manage` | ✅ | ✅ | ✅ | ✅ |
| `content:manage` | – | – | ✅ | ✅ |
| `review:moderate` | – | – | ✅ | ✅ |
| `deal:manage` | ✅ | ✅ | – | ✅ |
| `analytics:view` | – | ✅ | ✅ | ✅ |
| `compliance:manage` | – | – | – | ✅ |
| `webhook:manage` | – | – | – | ✅ |

¹ An agent may publish only when the tenant setting `settings.listings.agent_self_publish` is true. Marketing has `listing:manage` but not `listing:publish`.

Platform: `platform_support` holds `platform:tenant:view`; `platform_admin` holds `platform:tenant:view`, `platform:tenant:manage`, `platform:staff:manage`. Buyer and seller hold **no** permissions; every buyer-facing route authorises by ownership.

## 1.4 Visibility scope (layered on top of permissions)

Having a permission says what actions exist; **scope** says which rows. The backend applies scope inside repositories for listings, leads, appointments, deals and the per-listing analytics report:

| Role | Sees |
|---|---|
| agent | Rows assigned to or created by themselves |
| team_lead | Their own rows plus rows of every member of teams they lead |
| marketing, admin | Tenant-wide |

A row outside scope returns **404, never 403** (no existence oracle). The UI must treat a 404 on a detail page as "not found" and never as "you lack access".

Additional in-service gates the UI must mirror (these return 403 `permission-denied`):
- Only manager roles (team_lead, marketing, admin) may set `featured` on a listing, unassign a listing's agent (`agentId: null`), reassign a lead, assign a deal to another user, or publish an agent profile.
- Only `admin` may read or set deal commission figures, create/delete teams, or change a team's lead.
- An agent may only create listings assigned to themselves and may only create their own agent profile.

## 1.5 What each surface must do

- **Public site**: search and browse published listings (list, map, detail), agent directory and profiles, book a tour, request a valuation, mortgage calculator, CMS pages, blog, neighbourhood guides, gated market reports, legal pages, reviews, saved-search alert signup, cookie consent banner, analytics beacons, SEO assets (sitemap, JSON-LD, RSS).
- **Buyer account**: register/login, favourites, saved searches with alerts, my tours, in-app notifications with live push, notification preferences, profile, security (sessions, MFA), privacy (export, erasure).
- **Portal**: listings CRUD + workflow + media, CRM (leads, contacts, activities, assignment rules), agents & teams, tours & availability, deals, content CMS & blog, review moderation, analytics, user management, syndication, webhooks, compliance.
- **Platform console**: tenants lifecycle (create, domains, plan, billing, suspend, offboard, impersonate), platform staff, cross-tenant metrics, audit log.

---

# 2. Global structure

## 2.1 Hosting & tenant resolution

- Every API call to a tenant route must arrive with the agency's Host. In the browser this is automatic when the frontend is served from the agency domain. Any server-side rendering layer must forward the incoming `Host` header verbatim on its fetches, or no tenant resolves.
- The backend's CORS policy reflects an `Origin` **only if the Origin's host and the request's Host resolve to the same tenant**, or if the Origin is on the operator's static allowlist (`CORS_ORIGINS`, intended for the platform console and local dev). Credentials are allowed; `*` is never used. Consequence: the agency frontend must be served from a domain registered for that tenant (a `tenant_domains` row), and the platform console origin must be in `CORS_ORIGINS`.
- Local development: the demo tenant is `demo.localhost` (seeded by `scripts/seed_demo.py`); the frontend dev origin is expected at `http://demo.localhost:3000` and the platform console at `http://localhost:3000/platform/login`.

Tenant states the frontend must handle globally:

| Signal | Meaning | Required behaviour |
|---|---|---|
| 404 `not-found` on **every** route including `GET /site/config` | Host is not a registered domain | Neutral "site not found" page. Do not reveal the platform. |
| 402 `tenant-suspended` (Problem body) | Agency suspended (unpaid, trial lapsed, offboarding) | Full-page maintenance screen. Catch in the global error boundary — it can appear on any request at any time, including refresh. |
| WebSocket closed with code 1008 | Same as above, or a bad ticket | Stop reconnecting; re-fetch `/site/config` to decide which. |

## 2.2 API conventions

| Concern | Rule |
|---|---|
| Base path | `/api/v1` |
| JSON casing | camelCase in request and response bodies. Validation-error `loc` paths also use camelCase names. Query params: mostly camelCase, with the snake_case exceptions listed in the notation table. Problem bodies use `request_id` and `retry_after` (snake_case). |
| Ids | UUID strings (UUIDv7 — sortable, but never rely on that client-side) |
| Datetimes | ISO 8601 with offset, always UTC on output (`2026-09-03T10:00:00Z`). Send UTC. |
| Dates | `YYYY-MM-DD` (`date` fields, analytics windows, milestone due dates, availability exceptions) |
| Times | `HH:MM:SS` (availability rules), interpreted in the tenant's `settings.appointments.timezone` |
| Money | Output: decimal string. Input: number or numeric string, ≤ 2 dp (commission `rate` ≤ 3 dp). Currency: ISO-4217 uppercase, default `DZD`. Never do money arithmetic in floats. |
| Booleans | true/false, never 0/1 |
| i18n fields | Public endpoints: one negotiated string per field. Portal endpoints: `I18n` map. Supported locales `ar`, `fr`, `en`; default `fr`; fallback chain fr → en → ar (a missing locale never yields a hole on public reads). On write, an unknown locale key is a 422. |
| Locale negotiation (public reads) | `?locale=` beats `Accept-Language` beats the default. Send `?locale=` explicitly from the UI's language switcher; also set `Accept-Language` for SSR fetches. |
| Pagination | Cursor-based on every paginated list: send `cursor` (opaque string from the previous `nextCursor`) and `limit` (1–100, default 24). `nextCursor: null` means last page. A cursor minted under one `sort` is invalid under another → 400 `invalid-cursor`; reset the cursor whenever filters or sort change. `totalEstimate` may be null — render "many" rather than a number when null. Some endpoints return bare arrays (documented per endpoint) — those are unpaginated by design. |
| Idempotency | `Idempotency-Key: <uuid>` header is honoured on exactly three POSTs: `/leads/capture`, `/agents/{slug}/appointments`, `/platform/tenants/{id}/checkout`. Generate a fresh key per user intent, reuse it on retry. A replay returns the cached response byte-for-byte; a concurrent duplicate returns 409 `idempotency-key-in-flight` (retry after the first completes). |
| Request id | Every response carries `X-Request-ID`. Show it on error screens for support. |
| HTTP caching | Public listing detail, CMS pages and legal pages send `ETag`, `Cache-Control: public, s-maxage=60`, `Vary: Accept-Language, Origin`, and (listing detail) `Last-Modified`. Browsers handle conditional requests automatically; do not add a client cache layer that fights it. |
| Interactive docs | `GET /docs` (Swagger) is available outside production. |

## 2.3 Authentication flow (tenant plane)

Tokens: a short-lived **access token** (JWT, `expiresIn` seconds, default 900) returned in the JSON body, and a long-lived **refresh token** set by the server as an httpOnly cookie. The frontend never sees the refresh token.

| Item | Value |
|---|---|
| Header | `Authorization: Bearer <accessToken>` |
| Refresh cookie | name `refresh_token`, `HttpOnly`, `SameSite=Lax`, `Secure` outside local dev, **`Path=/api/v1/auth`**, max-age 30 days |
| Platform refresh cookie | same name, **`Path=/api/v1/platform/auth`** — the two planes can coexist in one browser and neither cookie is sent to the other's endpoints |

Sequence:

1. `POST /auth/login {email, password}` → either `TokenOut` (done) or `MfaRequiredOut` (`mfaRequired: true`, `mfaToken`, `expiresIn` ≈ 300). No cookie is set on the MFA-required response.
2. If MFA: `POST /auth/mfa/verify {mfaToken, code}` → `TokenOut`. **One ticket buys one guess**: a wrong code invalidates the ticket (401) and the user must log in again. Do not offer "try another code" on the same ticket.
3. Store the access token in memory only (not localStorage). Keep `expiresIn` and schedule a refresh ~60 s before expiry.
4. `POST /auth/refresh` (no body; cookie is sent automatically because the path matches) → new `TokenOut` and a rotated cookie. A 401 here means the session is gone (expired, revoked, or the refresh token was reused → the whole family was revoked) — clear state and go to login.
5. On any 401 from a data call: attempt one refresh, replay the request once, then log out.
6. `POST /auth/logout` (bearer required) → 204, denylists the access token immediately and revokes the presented refresh token; the server clears the cookie. `POST /auth/logout-all` → 204, kills every session of the user.
7. Impersonation tokens (platform → tenant) have no refresh cookie and die at their TTL; see §3.25.

The access token payload is not for the client to parse for authorisation decisions, but it is safe to decode for display: claims `sub` (user id), `role`, `tid` (tenant id, absent for platform staff), `jti`, `iat`, `exp`, and `imp` (impersonator id, only on impersonation sessions). Prefer the `user` object returned with every `TokenOut` (`AuthUserOut`: `id, tenantId, email, role, locale, emailVerifiedAt, mfaEnabled`).

Token pinning: a token minted on agency A is rejected (401) on agency B's host, and a tenant token is rejected on `/platform/*`. The frontend never needs to handle cross-tenant sessions.

Rate limits on auth (per tenant + IP, per action): login, register, password-reset, mfa-verify share a configurable budget (default 10/min); refresh gets 3×. Past the budget the response is 429 with `Retry-After`. Separately, account lockout after repeated failures is invisible to the client — every failure is the same 401 "Invalid email or password". Never tell the user an account is locked; say the credentials were rejected and suggest waiting or resetting.

## 2.4 Permission gating in the UI

- Gate **navigation and controls** client-side from `user.role` using the matrix in §1.3, so users do not see actions they cannot perform.
- The server remains the authority. Always handle 403 `permission-denied` on any write as a normal outcome (toast + revert optimistic state), because in-service gates (manager-only actions, admin-only commission, tenant setting `agent_self_publish`) cannot all be derived from the role alone.
- Scope cannot be computed client-side (team membership is server data). List endpoints are already scoped; a detail 404 is the scope signal.

Suggested helper: `can(permission)` = `ROLE_PERMISSIONS[user.role].includes(permission)`; `isManager` = role in {team_lead, marketing, admin}; `isAdmin` = role === admin.

## 2.5 State conventions for every data view

Every screen that fetches must implement the four states, and lists a fifth:

| State | Requirement |
|---|---|
| Loading | Skeleton of the eventual layout; never a blank region. Keep filters interactive. |
| Empty | Distinguish "no data yet" (call to action) from "no results for these filters" (clear-filters action). |
| Error | Show the Problem `title`/`detail`, the `X-Request-ID`, and a retry. For 402 and host-404 defer to the global handlers. |
| Success | Rendered data. |
| Stale / refetching (lists) | Keep the previous page visible while a refetch is in flight; disable the "load more" control during a page fetch. |

Write actions: disable the submit while in flight, show inline field errors from `errors[].loc`, show a summary for non-field problems, and never double-submit (use the Idempotency-Key on the three supported routes; elsewhere rely on the disabled button).

Async server work (media processing, report PDFs, agent photo) is **not** complete when the request returns. Poll the resource until a terminal status (documented per module).

## 2.6 Error contract

Every error body is `application/problem+json`:

```
{
  "type": "https://api.realestate.example/errors/<slug>",
  "title": "Human title",
  "status": 422,
  "instance": "/api/v1/portal/listings",
  "detail": "Safe, user-readable message (optional)",
  "request_id": "hex (optional)",
  "errors": [ { "type": "value_error", "loc": ["body", "contact"], "msg": "..." } ],   // 422 only
  "retry_after": 12                                                                     // 429 only
}
```

Slug → status mapping and the UI behaviour for each is in Appendix B. Key rules:
- Match on the **slug** (last path segment of `type`), not on `status` alone — 403 is both `permission-denied` and `quota-exceeded`; 422 is both `validation-error` and `breached-password`; 409 is both `conflict` and `idempotency-key-in-flight`.
- `errors[].loc` uses wire (camelCase) field names, prefixed with `body`/`query`/`path`. Map them to form fields.
- `detail` is written for end users and is safe to display verbatim.

## 2.7 Public form anti-spam contract (applies to every public write)

Every unauthenticated form that creates data carries two fields: `hp` (must be sent as an empty string; a hidden text input placed off-screen, not `display:none`, with `tabindex=-1` and `autocomplete=off`) and `renderedAt` (ISO datetime captured when the form mounted). The server rejects a submit less than 3 s after `renderedAt` and one older than 24 h with a 422 whose `errors[].loc` names `renderedAt`. **A submission the server judges to be a bot returns a normal success response with a fabricated id and persists nothing.** The UI therefore must not build any "was it saved?" logic on the returned id, and must re-capture `renderedAt` on remount (a form left open overnight must be refreshed).

Applies to: lead capture, WhatsApp click, tour booking, review submission, valuation completion, mortgage email, saved-search signup, report download.

## 2.8 Rate limits

All public write endpoints are rate-limited per tenant + client IP with a sliding window; a 429 carries `Retry-After` (seconds) in the header and `retry_after` in the body. Show a countdown on the submit button; never auto-retry a write. Full table in Appendix C.

## 2.9 Layout shells

**Public site shell**: header (agency name/logo from `/site/config`, primary nav: Buy, Rent, Agents, Guides, Blog, Valuation, Contact; language switcher ar/fr/en with RTL for `ar`; sign-in / account menu), footer (legal links from `GET /legal`, contact block from `settings.contact`, social), cookie banner (§3.9), WhatsApp floating action (§3.4) when the tenant has a number configured.

**Buyer account shell**: the public shell plus a left rail: Favourites, Saved searches, My tours, Notifications (with unread badge from `/me/notifications/unread-count` and live updates from the WebSocket), Settings (profile, security, notifications, privacy).

**Portal shell**: top bar (agency name, global search — a client-side router to Listings/Leads search, notification bell, user menu with impersonation banner when `imp` claim present), left nav built from permissions:

| Nav item | Visible when |
|---|---|
| Dashboard | always |
| Listings | `listing:manage` |
| Leads, Contacts | `lead:manage` (Contacts additionally needs `lead:view_all`) |
| Tours | `appointment:manage` |
| Deals | `deal:manage` |
| Agents, Teams | `agent:manage` (My profile: any portal role) |
| Content, Blog | `content:manage` |
| Reviews | `review:moderate` |
| Analytics | `analytics:view` (the "My listings performance" report: any portal role) |
| Users | `user:view` |
| Settings → Syndication | `listing:manage` |
| Settings → Assignment rules | `lead:assign` |
| Settings → Webhooks | `webhook:manage` |
| Settings → Compliance | `compliance:manage` |
| Settings → Security (MFA, sessions) | always |

**Platform shell**: Tenants, Staff (platform admin only), Metrics, Audit log, own security.

## 2.10 Shared components (behavioural spec)

- **Cursor list**: props `fetchPage(cursor, filters, sort)`; renders items, a "load more" (or infinite scroll) driven by `nextCursor`, resets on filter/sort change, keeps prior items during refetch, shows `totalEstimate` when non-null.
- **Status badge**: one component keyed by enum + domain (listing status, lead stage, appointment status, deal status, media status, review status, sync status, webhook delivery status).
- **I18n field editor**: tabs ar/fr/en over one input; shows which locales are filled; marks the default locale (fr) as required where the backend requires content (listing title, post title/body, page title, guide name, report title, category name).
- **Rich text editor**: outputs HTML restricted to the backend's sanitiser allowlist for blog bodies: `p, br, strong, em, b, i, u, ul, ol, li, h2, h3, h4, blockquote, a(href,title,target), img(src,alt,title)`; links get `rel="noopener noreferrer nofollow"` server-side; `javascript:` URLs are stripped. Anything else is silently removed on save — preview the sanitised result after save.
- **Direct-to-storage uploader**: generic three-step (request presign → PUT bytes to `uploadUrl` with the returned headers → confirm) with progress, cancel, and a poll-until-terminal phase. Used by listing media, agent photo, deal documents.
- **Capture form scaffold**: injects `hp` + `renderedAt`, the contact block (`firstName, lastName, email, phone, whatsapp, marketingConsent` — requires email **or** phone), UTM/page/referrer capture, and handles 429 countdowns.
- **Map**: pins/clusters renderer with a viewport → `inBbox` binder and a draw-polygon → `inPolygon` binder (see §3.3 for the encodings).
- **Address + point picker**: `AddressIn` fields plus a lat/lng pin (no geocoding exists server-side; the pin is the only geo signal).
- **Problem toast / inline error**: renders slug-aware messages per Appendix B.
- **Confirm dialog** for every destructive or irreversible action (delete, transition to a terminal state, erase account, offboard tenant).

## 2.11 Real-time & polling summary

| Mechanism | Where |
|---|---|
| WebSocket `/api/v1/ws/notifications?ticket=…` | Buyer account and portal notification bell (§3.10) |
| Poll every 2–3 s until terminal | Listing media after confirm; agent photo after confirm; market report after publish; deal document after confirm (synchronous but verify) |
| Refetch on focus | Leads inbox, tours agenda, deals list, review queue |
| No polling | Everything else — data is request/response |

---

# 3. Modules

Each module section is self-contained: it repeats the shapes it needs so it can be built without reading other sections. Shared conventions (auth header, Problem bodies, pagination, anti-spam fields) are in §2 and are referenced, not repeated.

---

## 3.1 Site bootstrap & tenant state

**Why it exists**: every surface on an agency domain needs the agency's identity, branding settings, plan and quotas before it can render anything. This is the first call of every page load.

### Screens
None of its own. Feeds the shells, the 404/402 pages, and the quota indicators.

### Endpoints

**`GET /site/config`** 🌐 — cached server-side 5 min.
Response `SiteConfigOut`:
```
{
  name: string, slug: string,
  settings: object,              // free-form agency settings, secrets scrubbed (see Appendix D)
  plan: "trial" | "starter" | "growth" | "enterprise" | string,
  usage:  { listingsCount: number, agentsCount: number, storageBytes: number, emailsSent: number },
  limits: { maxListings: number|null, maxAgents: number|null, storageGb: number|null, monthlyEmails: number|null }
}
```
`null` in `limits` means unlimited — render "Unlimited", never 0. `usage` may lag up to 5 minutes; use it for display only (the server enforces quotas at write time with 403 `quota-exceeded`).
Errors: 404 (unknown host), 402 (suspended).

**`GET /site/cookie-config`** 🌐 → `CookieConsentConfigOut | null` (see §3.9).

### Behaviour
- Fetch once per page load in the root layout; provide via context. Re-fetch after any portal write to tenant settings (there is none from the portal today except syndication; platform PATCHes invalidate the server cache).
- The settings namespaces the backend itself reads are listed in Appendix D. Read every key defensively — **GAP-20**: there is no schema for branding/theme; the frontend defines its own contract (e.g. `settings.branding.logoUrl`, `settings.branding.primaryColor`) and must tolerate absence.
- **GAP-21**: `plan`, `usage` and `limits` are served anonymously to every visitor. Do not render them on the public site; show them only in the portal (Settings → Plan & usage).

---

## 3.2 Authentication, session & account

**Why it exists**: sign-up and sign-in for buyers, sign-in for staff, second factor, session/device management, password recovery, email verification, profile.

### Screens

1. **Sign in** (`/login`)
   - Email, password, submit; "Forgot password"; "Create account"; social buttons rendered only from `GET /auth/oauth/providers`.
   - Actions: submit → §2.3 flow. On `MfaRequiredOut` navigate to the MFA step carrying `mfaToken` in memory (never in the URL).
   - States: loading; error 401 "Invalid email or password" (identical for unknown email, wrong password, disabled and locked accounts — do not differentiate); 429 with countdown; 422 field errors.
   - Success: route by role — buyer/seller → `/me`; portal roles → `/portal`.

2. **MFA code** (`/login/mfa`)
   - Six-digit code input (accepts 6–10 chars), submit, "start over".
   - `POST /auth/mfa/verify {mfaToken, code}` → `TokenOut`. Any 401 (expired ticket ~5 min, wrong code, invalid) → return to sign in with the message from `detail`; the ticket is single-use.

3. **Register** (`/register`)
   - Fields: email, password (8–128), role selector limited to `buyer_renter` | `seller` (any other role → 422 "created by an administrator"), firstName?, lastName?, phone?, locale (default `fr`).
   - `POST /auth/register` → 201 `TokenOut` (user is signed in immediately; refresh cookie set). Errors: 409 `conflict` "An account with this email already exists"; 422 `breached-password` → show "this password appeared in a data breach, choose another" and keep the form; 422 `validation-error`; 429.
   - After success: prompt to verify email (`emailVerifiedAt` is null). Email verification is only *enforced* for the "My tours" list, but surface the prompt on the account dashboard.

4. **Forgot / reset password** (`/password/forgot`, `/password/reset?token=`)
   - `POST /auth/password/forgot {email}` → 202 `{detail}` always (no enumeration). Show "if that address exists, an email is on its way".
   - `POST /auth/password/reset {token (16–128), newPassword (8–128)}` → 204. Errors: 401 "reset token is invalid or has expired"; 422 `breached-password` (the token is **not** consumed on this error — let the user pick another password on the same page); 429.

5. **Verify email** (`/verify-email?token=`)
   - `POST /auth/verify-email {token}` → 204. 401 on invalid/expired → offer "resend" which requires being signed in: `POST /auth/verify-email/request` → 202.

6. **Security settings** (`/me/settings/security` and `/portal/settings/security`, same component)
   - **Sessions**: `GET /auth/sessions` → `SessionOut[]` `{id, userAgent, ip, createdAt, lastUsedAt, expiresAt, current}`. Bare array, live sessions only. Render device rows; the `current: true` row cannot be revoked here (use logout). `DELETE /auth/sessions/{session_id}` → 204; 404 if not the caller's. "Sign out everywhere": `POST /auth/logout-all` → 204 then go to login.
   - **Two-factor**: `GET /auth/mfa/status` → `{enabled, enrolledAt}`. Enrol: `POST /auth/mfa/enrol` → 201 `{provisioningUri, secret}` — render `provisioningUri` as a QR code and `secret` as copyable text; then `POST /auth/mfa/enrol/confirm {code}` → 204 (401 wrong code → let them retry; the pending secret survives). Re-enrolling while enabled is allowed and does not disable the live factor until confirmed. Disable: `POST /auth/mfa/disable {password}` → 204; 401 "The password is incorrect".
   - For `admin` and `team_lead` show a persistent "enable two-factor" prompt while `enabled` is false (**GAP-10**: the API does not say which roles are expected to enrol; hardcode admin + team_lead).
   - Change password: **GAP-11** — no endpoint for a signed-in password change; link to the forgot-password flow.

7. **Profile** (`/me/settings/profile`, `/portal/settings/profile`)
   - `GET /users/me` → `UserOut` `{id, email, role, status, firstName, lastName, locale, phone, emailVerifiedAt, lastLoginAt, createdAt}`.
   - `PATCH /users/me {firstName?, lastName?, locale?, phone?}` → `UserOut`. Email and role are not editable here.
   - Locale change should also switch the UI language.

8. **Social login** (buttons on sign in)
   - `GET /auth/oauth/providers` → `{providers: string[]}` (currently `[]` unless Google credentials are configured; then `["google"]`).
   - `POST /auth/oauth/{provider}/start` → `{authorizationUrl, state}`; keep `state` in sessionStorage; redirect to `authorizationUrl`.
   - On return: `POST /auth/oauth/{provider}/callback {code, state}` → `TokenOut`. Errors: 501 `feature-not-configured` (hide the button — this only happens if the provider list was stale); 401 (state expired/invalid, provider failure, "This account is no longer active"); 409 (provider gave no email; email exists but provider did not verify it — tell the user to sign in with password; account inactive).

### Endpoint reference

| Method & path | Auth | Body → Response |
|---|---|---|
| `POST /auth/register` | 🌐 | `RegisterRequest` → 201 `TokenOut` + cookie |
| `POST /auth/login` | 🌐 | `{email, password}` → 200 `TokenOut` \| `MfaRequiredOut` |
| `POST /auth/mfa/verify` | 🌐 | `{mfaToken, code}` → 200 `TokenOut` |
| `POST /auth/refresh` | cookie | – → 200 `TokenOut` |
| `POST /auth/logout` | bearer | – → 204 |
| `POST /auth/logout-all` | bearer | – → 204 |
| `POST /auth/password/forgot` | 🌐 | `{email}` → 202 `{detail}` |
| `POST /auth/password/reset` | 🌐 | `{token, newPassword}` → 204 |
| `POST /auth/verify-email/request` | bearer | – → 202 `{detail}` |
| `POST /auth/verify-email` | 🌐 | `{token}` → 204 |
| `GET /auth/mfa/status` | bearer | → `{enabled, enrolledAt}` |
| `POST /auth/mfa/enrol` | bearer | → 201 `{provisioningUri, secret}` |
| `POST /auth/mfa/enrol/confirm` | bearer | `{code}` → 204 |
| `POST /auth/mfa/disable` | bearer | `{password}` → 204 |
| `GET /auth/sessions` | bearer | → `SessionOut[]` |
| `DELETE /auth/sessions/{session_id}` | bearer | → 204 |
| `GET /auth/oauth/providers` | 🌐 | → `{providers}` |
| `POST /auth/oauth/{provider}/start` | 🌐 | → `{authorizationUrl, state}` |
| `POST /auth/oauth/{provider}/callback` | 🌐 | `{code, state}` → `TokenOut` |
| `GET /users/me` | bearer | → `UserOut` |
| `PATCH /users/me` | bearer | `ProfileUpdate` → `UserOut` |

`TokenOut = { accessToken, tokenType: "bearer", expiresIn, user: AuthUserOut }`
`AuthUserOut = { id, tenantId, email, role, locale, emailVerifiedAt, mfaEnabled }`
`RegisterRequest = { email, password, role?, firstName?, lastName?, locale? ("fr"), phone? }`

Platform staff use the mirrored `/platform/auth/*` routes (§3.25).

---

## 3.3 Public listings, search & map

**Why it exists**: the agency's shop window — the highest-traffic pages and the entry point for every lead.

### Screens

1. **Home** (`/`) — hero search (purpose, property type, city, price range), featured/newest strip (`GET /listings?limit=8`; featured listings always lead the sort), agency review summary (`GET /reviews/summary`), agent strip (`GET /agents?limit=6`), latest posts (`GET /blog/posts?limit=3`), CTAs to valuation and tour booking.

2. **Search results** (`/listings`)
   - Layout: filter panel + result grid + map toggle. URL is the source of truth for every filter so results are shareable and SSR-able.
   - Filters (all optional query params, exact spelling): `purpose` (`sale|rent|rent_daily`), `propertyType` (enum, Appendix A), `priceMin`, `priceMax` (number, > 0, ≤ 999,999,999,999), `bedsMin`, `bathsMin` (0–100), `areaMin` (> 0), `city` (≤ 100 chars), `features` (repeatable: `features=pool&features=garage`; AND semantics), `q` (≤ 200 chars, full-text in the negotiated locale), geo (one of): `inBbox=minLon,minLat,maxLon,maxLat` · `near=lon,lat&radiusKm=` (radiusKm > 0, ≤ 100) · `inPolygon="lon lat,lon lat,…"` (≤ 4000 chars; ring must close), `sort` (`newest` default | `price_asc` | `price_desc` | `area_asc` | `area_desc`), `cursor`, `limit` (≤ 100), `locale`.
   - `GET /listings` → `Page<PublicListingOut>`. Cards use `cover` (thumb/card variants) and `blurhash` for the placeholder; `media` is null on list responses.
   - Sorting: `featured` rows always come first within any sort. Changing sort or any filter resets `cursor`.
   - States: loading skeleton grid; "no results" with clear-filters; error; 400 `invalid-cursor` → drop the cursor and refetch page 1.
   - Fire a `search` analytics event per query (§3.9) with `resultsCount` = `items.length` of the first page.

3. **Map search** (`/listings/map`)
   - Same filters; the viewport drives `inBbox` (debounce 300 ms); drawing a polygon sets `inPolygon` and clears `inBbox`.
   - `GET /listings/map` (same filters, no cursor/limit/sort) → `MapOut`:
     ```
     { clustered: boolean,
       pins:     [{ id, lat, lng, price: Money, status }],      // when ≤ 500 matches
       clusters: [{ lat, lng, count }] }                       // when > 500 matches
     ```
   - When `clustered` is true render cluster bubbles with `count`; clicking zooms in (which narrows `inBbox` and eventually yields pins). Pin click → card popover via `GET /listings/{id}`. The map response is cached server-side for 60 s.

4. **Listing detail** (`/listings/[referenceCode]`)
   - `GET /listings/{ref_or_id}` — accepts the reference code (`AGE-2026-00001`) or the UUID. Use the reference code in URLs.
   - Response `PublicListingOut` (full, detail variant):
     ```
     { id, referenceCode, purpose, propertyType, locale,
       title, description|null, price: Money, currency, pricePeriod: "month"|"day"|null, negotiable,
       beds|null, baths|null, areaBuilt: Money|null, areaLand: Money|null, floor|null, floorsTotal|null, yearBuilt|null,
       features: string[], address: object, location: {lat,lng}|null, publishedAt|null, featured,
       cover: PublicMediaOut|null,
       media: PublicMediaOut[],           // full gallery, ordered by position, ready media only
       jsonLd: object|null }              // schema.org RealEstateListing — inject verbatim as <script type="application/ld+json">
     PublicMediaOut = { id, kind: "photo"|"video"|"tour_3d"|"floorplan"|"doc", variants: { [name]: {url,width,height} },
                        blurhash|null, position, alt|null, isCover, embedUrl|null }
     ```
     Photo variant names: `thumb` (320w), `card` (640w), `gallery` (1280w), `full` (1920w), each in webp and jpeg (the map key is the variant name; check the URL extension for format). Videos and 3D tours are embeds: render `embedUrl` in an iframe (hosts are server-allowlisted: YouTube, Vimeo, Matterport). `doc` items are private and never appear publicly.
   - `address` is a free-form object with the keys the agency entered: `line1, line2, city, state, postalCode, country` (all optional).
   - Sections: gallery (with lightbox), price block (`pricePeriod` for rentals), key facts, description, features, map (if `location`), mortgage calculator inline for `purpose = sale` (§3.6), lead capture form (§3.4) with `listingId` and `source: "listing_form"`, WhatsApp button, book-a-tour CTA (**GAP-04**: the public listing carries no agent; link to the generic booking page and let the visitor pick an agent from the directory, or use the assigned agent once the gap is closed), share, favourite toggle (signed-in only, §3.10), similar listings (`GET /listings?propertyType=&city=&limit=4`).
   - Fire a `listing_view` event on mount.
   - Errors: 404 for unknown, unpublished or archived listings → "listing no longer available" page with a search CTA.
   - SEO: server-render title/description from the negotiated locale; `<link rel="alternate" hreflang>` for ar/fr/en with `?locale=`; canonical on the reference-code URL.

5. **SEO assets served by the backend** — link, do not rebuild: `GET /sitemap.xml` (published listings, pages, posts, guides on the request host), `GET /blog/rss.xml?locale=`, `GET /feeds/listings.xml` and `.csv` (published inventory for partner portals; anonymous). Point `robots.txt` at the sitemap URL.

### Edge cases
- `price` is a string; format with the locale and `currency`; for `rent`/`rent_daily` append `pricePeriod`.
- `areaBuilt`/`areaLand` are strings in m².
- A listing can be `featured` and appear in a different position than its date would suggest; show a "Featured" badge.
- The detail endpoint sends `ETag`/`Last-Modified`; if you implement client-side caching, honour 304s rather than storing copies.

---

## 3.4 Public lead capture & WhatsApp handoff

**Why it exists**: converting a visitor into a CRM lead is the product's reason to exist. Every public capture surface funnels into one CRM trunk (dedupe by email then phone, scoring, assignment, drip emails, speed-to-lead notification to the assigned agent).

### Screens / components

1. **Lead form** (embedded on listing detail, contact page, agent profile)
   - Fields: contact block (`firstName?, lastName?, email?, phone?, whatsapp?, marketingConsent` — **email or phone is required**, enforced server-side with a 422 whose `loc` is `["body","contact"]`), `message?` (≤ 2000), hidden: `listingId?`, `source` (required enum — use `listing_form` on a listing page, `other` elsewhere; the full enum is in Appendix A), `utmSource?/utmMedium?/utmCampaign?` (≤ 100, read from the landing URL and persisted in sessionStorage), `page?` (current path, ≤ 500), `referrer?` (≤ 500), `hp`, `renderedAt`.
   - `POST /leads/capture` → 201 `{id}`. Send `Idempotency-Key`.
   - Errors: 422 validation (field errors; "submitted too quickly" / "stale form" on `renderedAt`); 429 with countdown; 404 if `listingId` is not a published listing of this tenant (clear the hidden `listingId` and resubmit is acceptable).
   - Success: thank-you state; fire `form_submit` analytics event (`form: "lead"`); fire `form_start` on first focus.

2. **WhatsApp button** (floating on the public site; inline on listing detail and agent profiles)
   - Show only when either the listing's assigned agent has a WhatsApp number (unknown to the public API) **or** `settings.contact.whatsapp_number` is present in site config. In practice: always show on listing pages and handle the 409; show elsewhere only when the tenant setting exists.
   - On click, open a mini form (name + phone or email; consent) or, if the visitor already filled the lead form in this session, reuse that contact. Then `POST /leads/capture/whatsapp-click` with the same shape as lead capture minus `source` (fixed server-side to `whatsapp_click`) → 201 `{id, whatsappUrl}`; then `window.open(whatsappUrl)`. The lead lands in the CRM **before** the visitor leaves.
   - Errors: 409 `conflict` "This agency has not configured a WhatsApp contact number" → hide the button for the session; 422; 429.
   - The link is prefilled server-side with the listing reference and title; do not build your own `wa.me` URL.

### Endpoint reference

| Method & path | Body | Response | Errors |
|---|---|---|---|
| `POST /leads/capture` 🌐 5/min | `LeadCaptureCreate` | 201 `{id}` | 404, 422, 429 |
| `POST /leads/capture/whatsapp-click` 🌐 5/min (shared bucket) | `WhatsAppClickCreate` | 201 `{id, whatsappUrl}` | 404, 409, 422, 429 |

```
LeadCaptureCreate = { contact: ContactCaptureIn, listingId?, message?, utmSource?, utmMedium?, utmCampaign?, page?, referrer?, hp: "", renderedAt, source: LeadSource }
WhatsAppClickCreate = same minus source
ContactCaptureIn = { firstName?, lastName?, email?, phone?, whatsapp?, marketingConsent?: boolean }
```

---

## 3.5 Public agents, reviews & tour booking

**Why it exists**: people buy from people. The directory and profile pages present the agency's agents, their ratings and inventory, and let a visitor book a property visit into the agent's real calendar.

### Screens

1. **Agent directory** (`/agents`)
   - `GET /agents?specialty=&cursor=&limit=&locale=` → `Page<PublicAgentOut>`:
     ```
     { id, slug, displayName, locale, bio|null, specialties: string[], licenseNo|null, socials: {[network]: url},
       photoVariants: { avatar?: {url,width,height}, card?: {...} }, reviews: {count, average|null}|null }
     ```
   - Specialty filter chips from the fixed vocabulary: `residential_sales, residential_rentals, commercial, luxury, land, new_developments, off_plan, property_management, valuation, industrial` (**GAP-22**: not exposed by an endpoint; hardcode and localise).
   - Only published profiles with active accounts appear. Cards: photo (`card` variant, fallback initials), name, specialties, star average + count.

2. **Agent profile** (`/agents/[slug]`)
   - `GET /agents/{slug}` → `PublicAgentDetailOut` = card fields + `listings: PublicListingOut[]` (the agent's published listings, with covers).
   - `GET /agents/{slug}/reviews?cursor=&limit=` → `Page<PublicReviewOut>` `{id, agentUserId|null, rating (1–5), title|null, body, authorName, isVerified, createdAt}`.
   - Sections: header (photo `avatar`/`card`, name, licence, socials, WhatsApp/contact CTA → lead form with `source: "other"`), bio, specialties, review summary + list (load more), "Book a visit" (screen 3), listings grid, "Write a review" (screen 4).
   - 404 → "agent not found".

3. **Book a visit** (`/agents/[slug]/book?listing=`)
   - Step 1: date picker (today → +90 days). On date change: `GET /agents/{slug}/slots?date=YYYY-MM-DD` → `[{startAt, endAt}]` (UTC instants). Render in the visitor's local time **and** label the agency timezone from `settings.appointments.timezone` when it differs. Empty array → "no availability that day". Past slots are already excluded server-side. Dates beyond 90 days → 409; block them in the picker.
   - Step 2: pick a slot; contact block; `message?`; hidden `listingId?` (from the query, when arriving from a listing); `hp`, `renderedAt`.
   - `POST /agents/{slug}/appointments {contact, listingId?, message?, utm…, page?, referrer?, hp, renderedAt, startAt}` → 201 `{id, status: "requested", startAt, endAt}`. Send `Idempotency-Key`. `startAt` must equal a slot's `startAt` exactly (send the string you received).
   - Errors: 409 "This time slot is not available" → refetch slots and ask again; 404 agent unpublished; 422; 429 (5/min).
   - Success: "request received; the agent will confirm" with the time; explain that a confirmation email arrives; if signed in, link to My tours (which requires a verified email — say so).
   - The booking also creates a CRM lead assigned to that agent; nothing to do client-side.

4. **Write a review** (modal on profile, and `/reviews/new` for an agency-wide testimonial)
   - Fields: rating 1–5 (required), title? (≤ 200), body (1–4000), authorName (1–120), authorEmail? (≤ 320), hidden `agentSlug?` (omit for an agency-wide testimonial), `listingRef?` (≤ 120, a reference code or id), `hp`, `renderedAt`.
   - `POST /reviews` → 201 `{id, status: "pending"}`. Message: "thanks — reviews appear after moderation". Errors: 404 unknown/unpublished agent or listing; 422; 429 (5/min).

5. **Agency testimonials** (home, `/reviews`)
   - `GET /reviews?cursor=&limit=` → `Page<PublicReviewOut>` (only reviews with no agent). `GET /reviews/summary` → `{count, average|null}` across every approved review. `average` null → render "no reviews yet", not 0 stars.

### Endpoint reference

| Method & path | Response |
|---|---|
| `GET /agents` 🌐 | `Page<PublicAgentOut>` |
| `GET /agents/{slug}` 🌐 | `PublicAgentDetailOut` |
| `GET /agents/{slug}/reviews` 🌐 | `Page<PublicReviewOut>` |
| `GET /agents/{slug}/slots?date=` 🌐 | `SlotOut[]` |
| `POST /agents/{slug}/appointments` 🌐 5/min, Idempotency-Key | 201 `TourBookingOut` |
| `POST /reviews` 🌐 5/min | 201 `{id, status}` |
| `GET /reviews` 🌐 | `Page<PublicReviewOut>` |
| `GET /reviews/summary` 🌐 | `{count, average}` |

---

## 3.6 Seller tools: valuation wizard & mortgage calculator

**Why it exists**: seller acquisition (valuation) and buyer engagement (mortgage). Both mint CRM leads.

### Screens

1. **Valuation wizard** (`/estimate`, three steps; state held in memory plus the returned token in sessionStorage so a reload survives)
   - Step 1 — location: `street?` (≤ 200), `city` (required, ≤ 120), `postalCode?` (≤ 20), map pin `lat?/lng?`. Explain that a pin improves accuracy (it is the only geo signal; no geocoding). `POST /valuations` → 201 `{token}`.
   - Step 2 — property: `propertyType?` (enum), `areaBuilt?` (Money), `beds?`, `baths?` (0–100), `floor?` (−5..200), `yearBuilt?` (1800..2100), `condition?` (≤ 60), `notes?` (≤ 2000). `PATCH /valuations/{token}` → `ValuationDraftOut` `{address, propertyType, areaBuilt, beds, baths, floor, yearBuilt, details}`. Partial and repeatable — save on each field blur or on "Next".
   - Step 3 — contact: contact block (email or phone), `message?`, utm/page/referrer, `hp`, `renderedAt`. `POST /valuations/{token}/complete` → 200 `ValuationEstimateOut` `{id, estimateLow: Money|null, estimateHigh: Money|null, currency, compsCount, completedAt, disclaimer}`.
   - Result screen: when both bounds are present show the band and `compsCount` ("based on N comparable sales") and the `disclaimer` verbatim; when null show "not enough comparable data — an agent will contact you". Either way the lead exists.
   - Errors: 404 on any step → token invalid/expired/already completed → restart at step 1; 409 on complete → already completed (show result unavailable, restart); 422; 429 — all three steps share one 15/hour bucket, so save sparingly (on step change, not per keystroke).

2. **Mortgage calculator** (`/tools/mortgage`; also inline on sale listings)
   - Inputs: `price` (required, > 0), `downPayment?` (≥ 0), `annualRatePercent?` (0–100), `termYears?` (1–40). Omitted values fall back to the tenant's `settings.mortgage` defaults (rate 6.5 %, 25 years, 20 % down unless overridden) — show the effective values from the response, not your own defaults.
   - `POST /tools/mortgage-estimate` (60/min) → `{price, downPayment, loanAmount, annualRatePercent, termYears, monthlyPayment, totalPaid, totalInterest}` (all Money strings except `termYears`). Debounce recalculation 300 ms.
   - "Email me this estimate": contact block + the same inputs + `listingId?` + `hp`/`renderedAt`; `POST /tools/mortgage-estimate/email` (5/min) → 201 `{id, estimate}`. `contact.email` is required here (422 otherwise). The server recomputes; render `estimate` from the response. 404 if `listingId` is not a published listing.

---

## 3.7 Public content: pages, legal, guides, market reports, blog

**Why it exists**: the agency's editorial site — marketing pages built from blocks, versioned legal pages, neighbourhood guides with live listing auto-linking, gated market reports, and a blog.

### Screens

1. **CMS page** (`/[slug]`, catch-all after the fixed routes)
   - `GET /pages/{slug}?locale=` → `PublicPageOut` `{slug, title|null, blocks: [{type, data}], seoTitle|null, seoDescription|null, ogImage|null}`. Cached server-side 5 min; sends ETag.
   - Block renderer: the backend validates only the envelope; `type` ∈ `hero, richtext, listings_grid, cta, image, gallery, faq, stats, contact` and `data` is whatever the portal editor saved. **The frontend owns the `data` schema per block type** (define it once in a shared `blocks/` package used by both the public renderer and the portal editor). `listings_grid` should call `GET /listings` with the filters stored in `data`.
   - 404 → draft or unknown → site 404 page.
   - Preview: `/preview/[slug]?token=` → `GET /pages/{slug}/preview?token=` → same shape for a draft. Render with a "preview" banner and `noindex`. 404 on bad/foreign token.

2. **Legal** (`/legal/[kind]`, footer links)
   - `GET /legal` → `[{kind, version, effectiveAt}]` for the footer (kinds: `privacy, terms, fair_treatment, license_disclosure`). `GET /legal/{kind}?locale=` → `{kind, version, body|null, effectiveAt}`. Show version and effective date; body is trusted HTML/markdown from the agency (render as HTML).

3. **Neighbourhood guides** (`/guides`, `/guides/[slug]`)
   - `GET /guides?cursor=&limit=&locale=` → `Page<PublicGuideOut>` `{slug, name|null, body|null, boundary: [[[lon,lat],…],…]|null, seoTitle, seoDescription, ogImage, stats: object}`. `stats` is worker-computed nightly and may be `{}` or `{listingCount, medianPrice}` — render only keys present.
   - `GET /guides/{slug}?cursor=&limit=` → `{guide: PublicGuideOut, listings: PublicListingOut[], listingsNextCursor|null}` — the listings are those whose point lies inside the boundary, paginated by `cursor`/`listingsNextCursor`. Draw `boundary` (MultiPolygon rings, `[lon, lat]` order) on a map.

4. **Market reports** (`/reports/[slug]`)
   - `GET /reports/{slug}?locale=` → `{slug, title|null, stats: object, publishedAt|null, pdfReady: boolean}`. Render `stats` as the agency's compiled figures (free-form object; render key/value or chart what you recognise).
   - Download gate: when `pdfReady`, show a form (contact block with **email required**, `hp`, `renderedAt`, utm/page/referrer). `POST /reports/{slug}/download` → 200 `{downloadUrl}` (15-min presigned URL; open it immediately). Errors: 404 draft/unknown; 409 "not ready yet" (published but PDF still rendering — show "check back shortly"); 422. This endpoint has no per-endpoint rate limit (only the global per-IP budget), but it applies the honeypot/`renderedAt` rules.

5. **Blog** (`/blog`, `/blog/[slug]`, `/blog/category/[slug]`, `/blog/tag/[tag]`)
   - `GET /blog/categories?locale=` → `[{slug, name|null}]`.
   - `GET /blog/posts?category=&tag=&cursor=&limit=&locale=` → `Page<PublicPostOut>` `{slug, title|null, excerpt|null, body|null, tags: string[], coverImage|null, category: {slug,name}|null, publishedAt|null, seoTitle, seoDescription, ogImage}`. List responses include `body` too; ignore it on cards.
   - `GET /blog/posts/{slug}` → `PublicPostOut`. `body` is sanitised HTML — render as HTML. 404 for drafts/scheduled.
   - RSS link: `GET /blog/rss.xml?locale=`.

### Endpoint reference

| Method & path | Response |
|---|---|
| `GET /pages/{slug}` 🌐 | `PublicPageOut` |
| `GET /pages/{slug}/preview?token=` 🌐 | `PublicPageOut` |
| `GET /legal` 🌐 | `LegalIndexEntry[]` |
| `GET /legal/{kind}` 🌐 | `PublicLegalPageOut` |
| `GET /guides` 🌐 | `Page<PublicGuideOut>` |
| `GET /guides/{slug}` 🌐 | `PublicGuideDetailOut` |
| `GET /reports/{slug}` 🌐 | `PublicReportOut` |
| `POST /reports/{slug}/download` 🌐 | `{downloadUrl}` |
| `GET /blog/categories` 🌐 | `PublicCategoryOut[]` |
| `GET /blog/posts` 🌐 | `Page<PublicPostOut>` |
| `GET /blog/posts/{slug}` 🌐 | `PublicPostOut` |
| `GET /blog/rss.xml` 🌐 | XML |
| `GET /sitemap.xml` 🌐 | XML |

---

## 3.8 Anonymous saved-search alerts

**Why it exists**: a visitor who is not ready to register can still subscribe to new-listing alerts for a search; the confirmation doubles as marketing consent and creates a `search_signup` lead.

### Screens / components

1. **"Alert me" panel** on search results
   - Fields: `email` (required), `name?` (default "My search"), `frequency` (`instant|daily|weekly`, default per server), `locale?` (current UI locale — FTS terms replay under it), hidden `filters` = the current search filters object (same keys as the `GET /listings` query, e.g. `{purpose:"sale", city:"Algiers", priceMax:"30000000"}`), `hp`, `renderedAt`.
   - `POST /saved-searches` (5/min) → 201 `{id}` (fake on honeypot). Message: "check your inbox to confirm".
2. **Confirm** (`/saved-searches/confirm?token=`) → `POST /saved-searches/confirm {token}` → 200 `SavedSearchOut` `{id, name, filters, frequency, locale, isActive: true, lastRunAt, createdAt, updatedAt}`. Errors: 401 `unauthorized` "The confirmation token is invalid or has expired" (single-use — a second click on the same link also 401s; render "link invalid or expired; sign up again", and do not trigger the global sign-out handler for this 401).
3. **Unsubscribe** (`/saved-searches/unsubscribe?token=`) → `POST /saved-searches/unsubscribe {token}` → 204, idempotent (a repeat is still 204). 401 "The unsubscribe token is invalid" on a tampered token. Show a confirmation and a "manage in your account" link.

Signed-in users manage saved searches under `/me` (§3.10) — the anonymous flow is only for visitors without an account.

---

## 3.9 Cookie consent & analytics beacons

**Why it exists**: GDPR-style consent proof and the anonymous traffic firehose behind the portal analytics. Analytics ingestion is **gated by consent per session**, so the banner and the beacon must be built together.

### Cookie banner
- Config: `GET /site/cookie-config` → `{categories: object[], bannerCopy: object, isEnabled} | null`. `null` or `isEnabled: false` → no banner and no analytics beacons for cookie-bound sessions. `categories` and `bannerCopy` are free-form objects the portal editor writes — **the frontend defines their shape** (same rule as page blocks). Suggested: `categories: [{key: "analytics"|"marketing", label: I18n, description: I18n}]`, `bannerCopy: {title: I18n, body: I18n, accept: I18n, reject: I18n}`.
- Session id: generate a random ≤ 64-char id on first visit and persist in a first-party cookie or localStorage; it is the consent subject for anonymous visitors.
- Submit: `POST /consent {sessionId, choices: {necessary: true, analytics: bool, marketing: bool}}` (30/min) → 201 `ConsentRecordOut[]` (one per category: `{id, category, granted, source, legalPageId|null, legalVersion|null, createdAt}`). Persist the choices locally with the timestamp; re-show the banner if the legal privacy version (from `GET /legal`) changes.
- Errors: 409 if `sessionId` is missing (anonymous consent must be tied to a session); 422; 429.
- A withdrawal is a new POST with `granted: false` values — nothing is ever updated in place.
- Signed-in users: **GAP-05** — no authenticated consent read/write surface exists; keep using the session-keyed POST, and show the locally stored choices.

### Analytics beacons
- `POST /analytics/events {events: AnalyticsEventIn[]}` (1–50 per batch, 120/min) → 202 `{accepted}`. Batch client-side (flush every 5 s or 20 events, and on page hide via `sendBeacon`/`keepalive`).
- Event union, discriminated on `eventType`; common optional fields `sessionId?` (≤ 64), `listingId?`, `source?` (≤ 60):

| eventType | Extra fields |
|---|---|
| `listing_view` | `listingId` **required** |
| `search` | `query?` (≤ 200), `resultsCount?` (≥ 0) |
| `favorite` | `listingId` **required** |
| `form_start` | `form?` (≤ 60) |
| `form_submit` | `form?` (≤ 60) |
| `page_view` | `path?` (≤ 500) |

- Consent gate (server-side, invisible to the client — `accepted` never reveals a drop): a batch with **no** `sessionId` is accepted as fully anonymous; a batch carrying a `sessionId` is stored only if that session's latest `analytics` consent is `granted`. Therefore: send `sessionId` only after analytics consent is granted; omit it entirely otherwise (still allowed — anonymous counting), or send nothing if the tenant's banner is enabled and the visitor rejected.
- Unknown event types or extra fields are 422 — do not extend the payloads.

---

## 3.10 Buyer account (`/me`)

**Why it exists**: the signed-in visitor's own space. Every route here authorises by ownership (no permission), so it works for `buyer_renter`, `seller`, and also for staff accounts.

### Screens

1. **Dashboard** (`/me`)
   - Cards: favourites count (first page of `/me/favorites`), saved searches (`/me/saved-searches`), upcoming tours (`/me/appointments?upcomingOnly=true&limit=3`), unread notifications (`/me/notifications/unread-count`), email-verification prompt when `emailVerifiedAt` is null (with resend), MFA prompt is not needed for buyers.

2. **Favourites** (`/me/favorites`)
   - `GET /me/favorites?cursor=&limit=&locale=` → `Page<{favoritedAt, listing: PublicListingOut}>`; cards with cover. Listings that were unpublished drop out of the list automatically (the row survives and re-appears on relist).
   - Toggle anywhere on the site: `PUT /me/favorites/{listing_id}` → 204 (idempotent; 404 if not a published listing) and `DELETE /me/favorites/{listing_id}` → 204 (idempotent). Optimistic UI; on 401 open sign-in and replay. Fire a `favorite` analytics event on add.

3. **Saved searches** (`/me/searches`)
   - `GET /me/saved-searches` → `SavedSearchOut[]` (bare array, max 20). Row: name, human summary of `filters`, frequency, active toggle, last run, "open search" (rebuild the `/listings` URL from `filters`), edit, delete.
   - Create from the results page ("Save this search"): `POST /me/saved-searches {name (1–120), filters?: PublicListingFilters, frequency?: "instant"|"daily"|"weekly", locale?}` → 201 `SavedSearchOut`. 409 when at the 20 cap ("You can keep at most 20 saved searches").
   - `GET /me/saved-searches/{id}`, `PATCH /me/saved-searches/{id} {name?, filters?, frequency?, isActive?, locale?}` → `SavedSearchOut`, `DELETE` → 204. 404 for another user's row.
   - `filters` on output is the validated camelCase object you sent; `frequency: instant` means an email per newly published match, `daily`/`weekly` a digest.

4. **My tours** (`/me/tours`)
   - `GET /me/appointments?upcomingOnly=&cursor=&limit=` → `Page<MyAppointmentOut>` `{id, agentUserId, listingId|null, status, startAt, endAt, confirmedAt|null, createdAt}`.
   - **Requires a verified email**: an unverified account receives an empty 200 (not an error). Show the verification prompt prominently when `emailVerifiedAt` is null and the list is empty.
   - The join is by the account's email against CRM contacts, so tours booked with a different email do not appear — say so in the empty state.
   - Rows: status badge (`requested` → "awaiting confirmation", `confirmed`, `completed`, `cancelled`, `no_show`), time in local tz, listing link (`GET /listings/{listingId}` for the title/cover; **GAP-23**: the agent is only an id — no name/slug; render "your agent" until the gap is closed), no cancel action (**GAP-03**: no buyer-side cancel; show the agency phone/WhatsApp instead).

5. **Notifications** (`/me/notifications`; also the bell in every shell)
   - `GET /me/notifications?unreadOnly=&cursor=&limit=` → `Page<NotificationOut>` `{id, type, payload: object, readAt|null, createdAt}`. `GET /me/notifications/unread-count` → `{unread}`. `POST /me/notifications/mark-read {ids?: uuid[], all?: boolean}` → 204 (both omitted is a no-op).
   - Types and the payload keys the templates use (render the text client-side — **GAP-12**: the in-app row carries only `type` + `payload`, no rendered title/body; the server's templates are for email only):

| type | payload keys | Suggested title | Deep link |
|---|---|---|---|
| `lead_assigned` | `leadId` | New lead assigned to you | `/portal/leads/{leadId}` |
| `lead_escalated` | `leadId`, `minutes` | Unassigned lead needs attention | `/portal/leads/{leadId}` |
| `appointment_reminder` | `startAt`, `when` | Reminder: visit {when} | `/portal/tours` |
| `appointment_confirmed` | `startAt` | Your visit is confirmed | `/portal/tours` |
| `appointment_cancelled` | `startAt` | Your visit was cancelled | `/portal/tours` |
| `milestone_due` | `milestoneTitle`, `dealTitle`, `dueDate` | Deal milestone due | `/portal/deals` |

   All six current types target **staff** (**GAP-13**: a buyer's notification centre is empty today; build the component once, shared by both shells).
   - **Live push**: `POST /me/notifications/ws-ticket` → `{ticket, expiresIn: 60}`; immediately open `wss://<agency-host>/api/v1/ws/notifications?ticket=<ticket>`. Each text frame is JSON `{id, type, payload, createdAt}` — prepend it to the list, bump the badge, toast it. Tickets are single-use and tenant-pinned; on close, mint a new ticket and reconnect with backoff (1 s → 30 s); close code 1008 = bad ticket or suspended tenant → refetch `/site/config` before retrying. On reconnect, reconcile by refetching `unread-count` and the first page.
   - Preferences (`/me/settings/notifications`): `GET /me/notifications/preferences` → `{types: [{type, digestEligible, channels: [{channel, enabled}]}]}` over channels `in_app, email, sms, whatsapp`. `PUT /me/notifications/preferences {types: [{type, channels: [{channel, enabled}]}]}` (partial — only named pairs are written) → same shape. **GAP-07**: `sms` and `whatsapp` have no adapter (a send is logged as skipped); render them disabled with "coming soon". `digestEligible` is false for every type today; hide the quiet-hours copy.

6. **Privacy** (`/me/settings/privacy`)
   - Export: `GET /me/export` → `{subjectUserId, subjectEmail, exportedAt, sections: {account, crm, favorites, notifications, consent}}` (section keys are free-form). Offer "download as JSON" by serialising the response client-side.
   - Erase account: confirm dialog (explain: sign-out everywhere now, data purged after 30 days, cannot be undone from the UI — **GAP-14**: no cancel endpoint). `DELETE /me` → 202 `{requestId, purgeScheduledAt}`; the caller's tokens are revoked immediately — clear state and show a farewell page with the purge date. Idempotent (a repeat returns the pending request). `GET /me/dsr/{dsr_id}` → `{id, kind: "export"|"erasure", status: "pending"|"completed"|"cancelled", purgeScheduledAt, completedAt, result, createdAt}` is only reachable while the account still authenticates, so it is of limited use — do not build a screen around it.

### Endpoint reference (all 👤 bearer)

| Method & path | Body → Response |
|---|---|
| `GET /me/favorites` | → `Page<FavoriteItemOut>` |
| `PUT /me/favorites/{listing_id}` | → 204 |
| `DELETE /me/favorites/{listing_id}` | → 204 |
| `GET /me/saved-searches` | → `SavedSearchOut[]` |
| `POST /me/saved-searches` | `SavedSearchCreate` → 201 `SavedSearchOut` |
| `GET/PATCH/DELETE /me/saved-searches/{id}` | → `SavedSearchOut` / 204 |
| `GET /me/appointments` | → `Page<MyAppointmentOut>` |
| `GET /me/notifications` | → `Page<NotificationOut>` |
| `GET /me/notifications/unread-count` | → `{unread}` |
| `POST /me/notifications/mark-read` | `{ids?, all?}` → 204 |
| `POST /me/notifications/ws-ticket` | → `{ticket, expiresIn}` |
| `GET/PUT /me/notifications/preferences` | → `PreferencesOut` |
| `GET /me/export` | → `DataExportOut` |
| `DELETE /me` | → 202 `{requestId, purgeScheduledAt}` |
| `GET /me/dsr/{dsr_id}` | → `DsrRequestOut` |
| `WS /api/v1/ws/notifications?ticket=` | frames `{id, type, payload, createdAt}` |

---

## 3.11 Portal shell & authorisation model

**Why it exists**: the agency back-office. One shell, navigation and controls derived from the permission matrix (§1.3), data reach derived from server-side scope (§1.4).

### Screens
1. **Portal sign-in** — the shared sign-in component (§3.2) at `/portal/login`; after login, non-portal roles (buyer/seller) are redirected to `/me`.
2. **Dashboard** (`/portal`) — a composition, no dedicated endpoint:
   - Quota card from `GET /site/config` (`usage` vs `limits`; warn at 80 %; `null` = unlimited).
   - "My inbox": `GET /portal/leads?stage=new&limit=5`, `GET /portal/appointments?status=requested&limit=5`.
   - Analytics tiles for `analytics:view` roles: `GET /portal/analytics/traffic` (30-day default) and `/lead-funnel`.
   - Own performance for every role: `GET /portal/analytics/listing-performance`.
   - Team lead / admin: `GET /portal/reviews?status=pending&limit=1` (badge count from `totalEstimate`).
3. **Impersonation banner** — when the decoded access token has an `imp` claim (§3.25): a fixed banner "You are viewing {agency} as platform staff · session ends at {exp}"; hide destructive actions? No — the backend allows everything the impersonated admin can do; keep the banner visible on every screen and stop the session at `exp` (no refresh exists).

### Behaviour
- Route guard: no token → `/portal/login`; role ∉ portal roles → `/me`; permission missing for a page → render an in-app "not authorised" page (do not call the API).
- Every list is already scoped by the server; never add a client-side "mine only" filter that hides rows the server chose to return.

---

## 3.12 Portal — Listings

**Why it exists**: inventory management — create, edit, photograph, publish and retire listings; the workflow is a state machine enforced server-side.

### Listing status machine (enforced by `POST …/transition`)

| From | To |
|---|---|
| `draft` | `review`, `published`, `archived` |
| `review` | `draft`, `published`, `archived` |
| `published` | `reserved`, `sold`, `rented`, `archived` |
| `reserved` | `published`, `sold`, `rented`, `archived` |
| `sold` | `archived` |
| `rented` | `archived` |
| `archived` | `draft` (relist) |

Publishing requires `listing:publish` **or** role `agent` with tenant setting `settings.listings.agent_self_publish` true; otherwise 403 "Publishing requires review by someone with publish rights" — offer "Send to review" (`review`) instead. Invalid transition → 409 with a message naming both states. Delete is only allowed from `draft`, `review`, `archived` (409 "Archive this listing before deleting it").

### Screens

1. **Listings list** (`/portal/listings`) 🔒 `listing:manage`
   - Toolbar: keyword search `q` (matches reference code, title in any locale, city — ILIKE, ≤ 200 chars), status filter (`status`), sort (`newest` default | `updated` | `price_asc` | `price_desc`), "New listing".
   - `GET /portal/listings?status=&q=&sort=&cursor=&limit=` → `Page<ListingOut>`. Reset cursor when `q`/`status`/`sort` change (a cursor minted under another sort is 400 `invalid-cursor`).
   - Columns: cover (needs `GET /portal/listings/{id}/media` per row — **GAP-15**: `ListingOut` has no cover; either fetch media lazily for visible rows or show a placeholder), reference code, title (default locale with fallback), status badge, price, agent (resolve `agentId` against `GET /users` for admins; agents see their own), `featured` star, `staleFlaggedAt` warning ("stale — expired or unchanged for 90 days"), `viewCount`, `updatedAt`.
   - Scope: agents see own; team leads their team; marketing/admin all. **GAP-16**: no `agentId` filter on this list — a team lead cannot filter by member.
   - Row actions: open, duplicate, transition menu (built from the machine above), delete (when allowed).

2. **Create / edit** (`/portal/listings/new`, `/portal/listings/[id]`)
   - `POST /portal/listings` `ListingCreate` → 201 `ListingOut` (status `draft`, server-minted `referenceCode`); `PATCH /portal/listings/{id}` `ListingUpdate` → `ListingOut`; `GET /portal/listings/{id}` → `ListingOut`.
   - Form fields (create): `purpose` (required, **immutable after create** — not in `ListingUpdate`), `propertyType` (required), `title: I18n` (required; at least the default locale), `description?: I18n`, `price` (required, > 0), `currency?` (`^[A-Z]{3}$`, default DZD), `negotiable?`, `beds?`/`baths?` (0–100), `areaBuilt?`/`areaLand?` (> 0), `floor?` (−5..200), `floorsTotal?` (1..200), `yearBuilt?` (≥ 1800), `features?: string[]` (free-form tags, lowercase them), `address?: {line1?, line2?, city?, state?, postalCode?, country? (2 letters)}`, `location?: {lat, lng}` (map pin), `agentId?` (agents: only themselves — 403 otherwise; managers: any active agent — 409 if inactive/unknown; omitted = the creator), `expiresAt?`.
   - Update-only: `featured?` (managers only → 403 for agents), `agentId: null` to unassign (managers only → 403). Sending explicit `null` for a required column (e.g. `price: null`) is a 422.
   - `ListingOut`:
     ```
     { id, referenceCode, agentId|null, status, purpose, propertyType, title: I18n, description: I18n,
       price: Money, currency, pricePeriod: "month"|"day"|null, negotiable, beds, baths, areaBuilt, areaLand, floor, floorsTotal, yearBuilt,
       features: string[], address: object, location: {lat,lng}|null, publishedAt, expiresAt, staleFlaggedAt, viewCount, featured,
       createdBy|null, createdAt, updatedAt }
     ```
   - Errors: 403 `quota-exceeded` on create when the plan's `maxListings` is reached (show the plan card); 422 field errors (`loc` like `["body","title"]`, message "unsupported locale keys" / decimal parsing); 404 out of scope.
   - Any PATCH clears `staleFlaggedAt` server-side; reflect it after save.
   - **AI description draft**: button "Draft description" → `POST /portal/listings/{id}/generate-description {locales?: ["ar","fr","en"], tone?}` → `{description: I18n, model}`. Show as a **draft** in the editor with "apply" per locale; nothing is saved until the user saves the form (the endpoint never persists). 503 `upstream-unavailable` → "AI service unavailable, try again"; 422 unknown locale. Expect 5–30 s latency; show a spinner and allow cancel.
   - Tabs on the edit page: Details · Media (§3.13) · Workflow & history · Syndication (§3.22) · Performance (`/portal/analytics/listing-performance` filtered client-side to this id).

3. **Workflow & history tab**
   - `POST /portal/listings/{id}/transition {toStatus}` → `ListingOut`. Confirm dialog for `sold`, `rented`, `archived`. On entering `published` the server sets `publishedAt`, fans out saved-search alerts, syndication and webhooks — nothing to do client-side.
   - `GET /portal/listings/{id}/history` → `[{id, fromStatus, toStatus, changedBy|null, createdAt}]` (bare array, newest last — sort as you like; `changedBy` null = system).

4. **Duplicate**: `POST /portal/listings/{id}/duplicate` → 201 a new `draft` copy with a new reference code (media is not copied). Navigate to it. 403 `quota-exceeded` possible.

5. **Delete**: `DELETE /portal/listings/{id}` → 204; releases a listing quota slot. 409 when the status forbids it.

### Endpoint reference (all 🔒 `listing:manage`, scoped)

| Method & path | Body → Response |
|---|---|
| `GET /portal/listings` | → `Page<ListingOut>` |
| `POST /portal/listings` | `ListingCreate` → 201 `ListingOut` |
| `GET /portal/listings/{listing_id}` | → `ListingOut` |
| `PATCH /portal/listings/{listing_id}` | `ListingUpdate` → `ListingOut` |
| `DELETE /portal/listings/{listing_id}` | → 204 |
| `POST /portal/listings/{listing_id}/transition` | `{toStatus}` → `ListingOut` |
| `POST /portal/listings/{listing_id}/duplicate` | → 201 `ListingOut` |
| `POST /portal/listings/{listing_id}/generate-description` | `{locales?, tone?}` → `{description, model}` |
| `GET /portal/listings/{listing_id}/history` | → `StatusHistoryOut[]` |

---

## 3.13 Portal — Media

**Why it exists**: photos, floor plans, documents and video/3D embeds per listing. Files go **directly to object storage** via presigned URLs; the API never receives bytes. Processing (variants, blurhash, EXIF stripping) is asynchronous.

### Media lifecycle
`pending` (presign issued) → `processing` (confirmed) → `ready` | `failed` (validation: wrong magic bytes vs declared type, oversize, corrupt — permanent, `error` set; the original is deleted). Embeds are `ready` immediately.

### Screen: Media tab on the listing editor 🔒 `listing:manage`

- `GET /portal/listings/{listing_id}/media` → `MediaOut[]` (bare array, by `position`):
  ```
  { id, listingId, kind: "photo"|"video"|"tour_3d"|"floorplan"|"doc", status, contentType|null, sizeBytes|null,
    variants: {[name]: {url,width,height}}, blurhash|null, position, altText: I18n, isCover, embedUrl|null, error|null, createdAt }
  ```
- **Upload (photos, floorplans, docs)** — three steps per file, run in parallel for multi-select with a concurrency of 3:
  1. `POST /portal/listings/{listing_id}/media/uploads {kind, contentType, sizeBytes, altText?}` → 201 `{media: MediaOut(pending), uploadUrl, uploadHeaders, expiresInSeconds}`. `sizeBytes` must be the real byte length (the worker HEAD-checks it). Errors: 403 `quota-exceeded` for a file over the limit (25 MB default) or when the listing has reached its photo quota (50 default, tenant-overridable via `settings.media.max_photos_per_listing`) or the plan's storage quota; 422 (kind `video`/`tour_3d` are not uploadable — use embeds).
  2. `PUT <uploadUrl>` with the raw file body and **exactly** the `uploadHeaders` (typically `Content-Type`). The URL expires in `expiresInSeconds` (900). Show per-file progress; a failed PUT means simply retry step 1.
  3. `POST /portal/media/{media_id}/confirm` → 202 `MediaOut(status: processing)`. 409 if already confirmed ("This upload is already 'processing'").
  Then poll `GET /portal/listings/{listing_id}/media` every 2 s until every item is `ready`/`failed` (stop after 2 min and show "still processing"). Render `failed` items with `error` and a delete button.
- **Embeds**: `POST /portal/listings/{listing_id}/media/embeds {kind: "video"|"tour_3d", url (≤ 500), altText?}` → 201 `MediaOut`. Allowed hosts — video: `youtube.com`, `www.youtube.com`, `youtu.be`, `vimeo.com`, `player.vimeo.com`; 3D: `my.matterport.com`. Anything else is 422 "embed host not allowed".
- **Reorder / alt / cover**: `PATCH /portal/media/{media_id} {position? (0–500), altText?, isCover?}` → `MediaOut`. Drag-drop reorder fires one PATCH per moved item (**GAP-17**: no bulk reorder). Setting `isCover: true` on a non-ready or non-photo item → 409 "Only a processed photo can be the cover"; the server enforces at most one cover.
- **Delete**: `DELETE /portal/media/{media_id}` → 204 (storage cleanup is asynchronous).
- **Download private files** (`floorplan`, `doc`, which are never public): `GET /portal/media/{media_id}/download` → `{downloadUrl, expiresInSeconds}` (15-min presigned GET). 409 "This media has no downloadable file" for embeds/unprocessed items.

### States
Per file: queued → uploading (progress) → processing (spinner) → ready (thumbnail from `variants.thumb`) / failed (error text). Gallery empty state: drop zone with the accepted kinds and limits.

---

## 3.14 Portal — CRM (leads, contacts, activities, assignment)

**Why it exists**: the pipeline. Every public capture surface creates a lead; agents work leads through stages, log touches, and managers steer routing.

### Concepts
- A **lead** always belongs to a **contact** (deduped by email, then phone). Leads carry a `source`, a `stage`, a computed `score` (0–100), an assigned `agentId` and an optional `listingId`.
- **Stage** moves through `POST …/stage`; there is no fixed graph — any stage may move to any other, except that `lost` requires `lostReason` (409 otherwise). Moving out of `new/contacted/qualified` stops the automated drip emails.
- **Activities** are the append-only timeline. Logging a `note`, `call`, `email` or `sms` as an agent stamps `firstResponseAt` (speed-to-lead) and stops the drip.
- **Scoring** (display only): source weight + listing attached + engagement − recency decay − 15 per no-show, clamped 0–100.

### Screens

1. **Leads inbox** (`/portal/leads`) 🔒 `lead:manage`
   - Filters (exact params): `stage`, `agent_id`, `source`, `listing_id`, `cursor`, `limit`. No keyword search (**GAP-18**).
   - `GET /portal/leads` → `Page<LeadOut>`:
     ```
     { id, contactId, listingId|null, agentId|null, source, sourceMeta: object, stage, score, lostReason|null, firstResponseAt|null, createdAt, updatedAt }
     ```
   - **GAP-01 (highest impact)**: `LeadOut` has no contact name/email/phone, and `GET /portal/contacts/{id}` requires `lead:view_all`, which agents lack. Until fixed, agents' inbox rows show source, stage, score, listing and age; the name appears only on the detail page (which embeds the contact). Managers can resolve contacts row-by-row (cache by `contactId`).
   - Row: stage badge, score meter, source icon, listing reference (resolve via `GET /portal/listings/{listingId}`, cached), assigned agent, "unanswered for" (`firstResponseAt` null → age), `sourceMeta` (utm/page/referrer) in a tooltip.
   - Views: table and kanban by stage (drag to a column = stage transition; `lost` prompts for a reason).
   - Scope: agent → own; team lead → team; marketing/admin → all. Refetch on window focus; the WebSocket `lead_assigned` event should also trigger a refetch.
   - "Log a lead" (manual entry, e.g. phone-in): `POST /portal/leads {contactId? | contact?: ContactCaptureIn, listingId?, source (enum, e.g. "phone"), agentId?}` → 201 `LeadDetailOut`. Exactly one of `contactId`/`contact` (422 otherwise); 404 unknown contact; 409 inactive agent; agents can only assign to themselves.

2. **Lead detail** (`/portal/leads/[id]`) 🔒 `lead:manage`
   - `GET /portal/leads/{lead_id}` → `LeadDetailOut` = `LeadOut` + `contact: ContactOut` `{id, firstName, lastName, email, phone, whatsapp, consent: object, tags: string[], notes|null, createdAt, updatedAt}`.
   - Header: name, contact channels (tel:/mailto:/wa.me links), stage selector, score, source, assigned agent (reassign control for managers only: `PATCH /portal/leads/{id} {agentId}` → 403 "Only managers can reassign a lead" for agents; 409 inactive agent), linked listing (`PATCH {listingId}`).
   - Stage change: `POST /portal/leads/{lead_id}/stage {toStage, lostReason?}` → `LeadOut`. `lost` requires a reason (409). Show the transition as an activity.
   - Timeline: `GET /portal/leads/{lead_id}/activities` → `ActivityOut[]` `{id, leadId, actorId|null, type, payload: object, createdAt}` (bare array). Types: `note, call, email, sms, status_change, assignment, tour, no_show, system`. Render by type: `note.payload.text`; `status_change.payload {from, to}`; `assignment`; `tour`; `no_show`; `system` (valuation/report payloads — render key/values); `actorId` null = system.
   - Log activity: `POST /portal/leads/{lead_id}/activities {type, payload?}` → 201. Offer `note` (`{text}`), `call`, `email`, `sms` with a free-text payload; the other types are system-written (**Assumption A-1**: the service accepts any type from the client; keep the UI to the four human types).
   - Related: appointments for this lead (`GET /portal/appointments` has no `lead_id` filter — **GAP-19**; show the "book/confirm tour" link instead), deals (`POST /portal/deals` prefilled with `leadId`, `contactId`, `listingId`).

3. **Contact** (`/portal/contacts/[id]`) 🔒 `lead:view_all`
   - `GET /portal/contacts/{contact_id}` → `ContactOut`; `PATCH /portal/contacts/{contact_id} {firstName?, lastName?, email?, phone?, whatsapp?, consent?: object, tags?, notes?}` → `ContactOut` (email is normalised lowercase; duplicate email → 409).
   - `GET /portal/contacts/{contact_id}/timeline` → `{contact, leads: LeadOut[], entries: [{kind: "lead_created"|"activity", at, leadId, activity?: ActivityOut, leadStage?}]}` — one merged chronological feed across the contact's leads.
   - **GAP-08**: there is no contacts list/search endpoint; the Contacts nav item can only open a contact from a lead. Build it as a detail page reached from leads, not as a directory.
   - `consent` is a free-form object the capture surfaces fill (e.g. `{marketing: {granted, at, source}}`); render keys present, allow manager edits as raw JSON.

4. **Assignment rules** (`/portal/settings/assignment`) 🔒 `lead:assign`
   - `GET /portal/leads/assignment-rule` → `{id, strategy, config: object, createdAt, updatedAt}`; 404 when none is configured → show the empty state with defaults (the effective default strategy is `listing_agent`).
   - `PUT /portal/leads/assignment-rule {strategy: "listing_agent"|"round_robin"|"territory", config?: {agentPool?: uuid[], maxOpenLeadsPerAgent?: ≥ 1}}` → `AssignmentRuleOut`.
   - Explain each: `listing_agent` (lead goes to the listing's assigned agent; unassigned when there is no listing), `round_robin` (least-loaded over `agentPool` or all active agents, optional cap), `territory` (agent whose published service area contains the listing's point; 409 when no agent has service-area data yet). Agent pool picker from `GET /users` filtered to `role = agent` (admins) or `GET /portal/agents` (agent:manage).
   - Note: leads unassigned for 30 min escalate to admins automatically (in-app + email).

### Endpoint reference

| Method & path | Perm | Body → Response |
|---|---|---|
| `GET /portal/leads` | `lead:manage` | → `Page<LeadOut>` |
| `POST /portal/leads` | `lead:manage` | `LeadCreate` → 201 `LeadDetailOut` |
| `GET /portal/leads/{lead_id}` | `lead:manage` | → `LeadDetailOut` |
| `PATCH /portal/leads/{lead_id}` | `lead:manage` (+manager for `agentId`) | `{agentId?, listingId?}` → `LeadOut` |
| `POST /portal/leads/{lead_id}/stage` | `lead:manage` | `{toStage, lostReason?}` → `LeadOut` |
| `GET /portal/leads/{lead_id}/activities` | `lead:manage` | → `ActivityOut[]` |
| `POST /portal/leads/{lead_id}/activities` | `lead:manage` | `{type, payload?}` → 201 `ActivityOut` |
| `GET /portal/contacts/{contact_id}` | `lead:view_all` | → `ContactOut` |
| `PATCH /portal/contacts/{contact_id}` | `lead:view_all` | `ContactUpdate` → `ContactOut` |
| `GET /portal/contacts/{contact_id}/timeline` | `lead:view_all` | → `ContactTimelineOut` |
| `GET /portal/leads/assignment-rule` | `lead:assign` | → `AssignmentRuleOut` (404 if none) |
| `PUT /portal/leads/assignment-rule` | `lead:assign` | `AssignmentRuleUpdate` → `AssignmentRuleOut` |

---

## 3.15 Portal — Agents & teams

**Why it exists**: the agency's public-facing people (profiles with photo, bio, specialties, service areas) and the internal team structure that drives team-lead visibility.

### Screens

1. **My profile** (`/portal/agents/me`) — any portal role
   - `GET /portal/agents/me` → `AgentProfileOut`; 404 "You do not have an agent profile yet" → show a "Create my profile" form.
   - `AgentProfileOut`:
     ```
     { id, userId, slug, bio: I18n, specialties: string[], serviceAreas: [[[lon,lat],…],…]|null, licenseNo|null, whatsappNumber|null,
       socials: {[network]: url}, isPublished, photoStatus: "pending"|"processing"|"ready"|"failed"|null,
       photoVariants: {avatar?: {url,width,height}, card?: {…}}, photoError|null, createdAt, updatedAt }
     ```
   - Create: `POST /portal/agents {userId? (managers only; agents omit = self), slug (2–120, ^[a-z0-9]+(-[a-z0-9]+)*$), bio?: I18n, specialties?: vocab[], serviceAreas?: MultiPolygon rings, licenseNo? (≤ 100), whatsappNumber? (E.164 ^\+[1-9]\d{6,14}$), socials?}` → 201. Errors: 403 (agent creating for another user; `quota-exceeded` when the plan's `maxAgents` is reached); 409 (slug or user already has a profile; user not an agent/team-lead account; inactive user).
   - Edit: `PATCH /portal/agents/{profile_id}` same fields + `isPublished?` (managers only → 403 "Only managers can publish an agent profile"). Slug conflict → 409.
   - Service areas: a polygon-drawing map producing rings of `[lon, lat]` pairs; the server closes/validates rings (422 on bad geometry). These areas drive the `territory` assignment strategy and never appear publicly.
   - Photo: `POST /portal/agents/{profile_id}/photo/uploads {contentType, sizeBytes}` → 201 `{profile, uploadUrl, uploadHeaders, expiresInSeconds}` → PUT the file → `POST /portal/agents/{profile_id}/photo/confirm` → 202 `AgentProfileOut(photoStatus: processing)` → poll `GET /portal/agents/{profile_id}` until `ready`/`failed` (`photoError`). 409 "Files larger than N MB" / "There is no pending photo upload to confirm". Variants: `avatar` 320w, `card` 640w.
   - Public preview link: `/agents/{slug}` (only resolves when `isPublished` and the account is active).

2. **Agent roster** (`/portal/agents`) 🔒 `agent:manage`
   - `GET /portal/agents` → `AgentProfileOut[]` (bare array, unpaginated). Columns: photo, name (resolve `userId` via `GET /users` — admin only; others render the slug), slug, published toggle (managers), specialties, WhatsApp set?, photo status.
   - Row: open (`GET /portal/agents/{profile_id}`), edit, delete (`DELETE` → 204, releases an agent quota slot; own or manager).
   - Stats drawer: `GET /portal/agents/{profile_id}/stats` → `{userId, listingsByStatus: {[status]: n}, leadsByStage: {[stage]: n}, avgFirstResponseSeconds|null, reviews: {count, average|null}}` (own profile or `agent:manage`).

3. **Teams** (`/portal/teams`) 🔒 `agent:manage` (router-wide), with admin-only actions inside
   - `GET /portal/teams` → `TeamOut[]` `{id, name, leadUserId|null, createdAt, updatedAt}` (bare array).
   - Create (admin only, else 403 "Only admins can create teams"): `POST /portal/teams {name (1–120), leadUserId?}` → 201. 409 if the lead is not an active agent/team-lead account.
   - Detail: `GET /portal/teams/{team_id}` → `TeamDetailOut` = `TeamOut` + `members: [{userId, roleInTeam|null, createdAt}]`. A team lead who does not lead this team gets 404.
   - Rename / change lead: `PATCH /portal/teams/{team_id} {name?, leadUserId?}` (changing the lead is admin-only → 403). Delete: admin-only `DELETE` → 204.
   - Members: `GET /portal/teams/{team_id}/members` → `TeamMemberOut[]`; `POST /portal/teams/{team_id}/members {userId, roleInTeam? (≤ 40)}` → 201 (409 "already a member" / "must be active agent or team-lead accounts"); `DELETE /portal/teams/{team_id}/members/{user_id}` → 204 (404 not a member). Membership can be managed by an admin or by that team's lead.
   - Member picker: admins use `GET /users`; team leads (no `user:view`) use `GET /portal/agents` and map `userId` (**GAP-24**: a team lead cannot see user names without `user:view`; display slugs).

### Endpoint reference

| Method & path | Perm |
|---|---|
| `GET /portal/agents` | `agent:manage` |
| `POST /portal/agents` | self or `agent:manage` |
| `GET /portal/agents/me` | any |
| `GET/PATCH/DELETE /portal/agents/{profile_id}` | own or `agent:manage` (`isPublished`: managers) |
| `POST /portal/agents/{profile_id}/photo/uploads` · `…/photo/confirm` | own or `agent:manage` |
| `GET /portal/agents/{profile_id}/stats` | own or `agent:manage` |
| `GET/POST /portal/teams` · `GET/PATCH/DELETE /portal/teams/{team_id}` | `agent:manage` (+admin gates) |
| `GET/POST /portal/teams/{team_id}/members` · `DELETE …/members/{user_id}` | `agent:manage` (admin or that team's lead) |

---

## 3.16 Portal — Tours & availability

**Why it exists**: the agent's agenda of visitor-booked property visits, their weekly availability template, and a calendar feed.

### Appointment status machine

| From | To |
|---|---|
| `requested` | `confirmed`, `cancelled` |
| `confirmed` | `completed`, `cancelled`, `no_show` |
| `completed`, `cancelled`, `no_show` | (terminal) |

`confirmed`/`cancelled` email the visitor; `no_show` logs a −15 score activity on the linked lead. Invalid moves → 409.

### Screens

1. **Agenda** (`/portal/tours`) 🔒 `appointment:manage`
   - Views: list (upcoming first) and week calendar. Filters (exact params): `status`, `start_from`, `start_to` (ISO datetimes), `cursor`, `limit`. For a calendar week send `start_from`/`start_to` for the visible range and `limit=100`.
   - `GET /portal/appointments` → `Page<AppointmentOut>`:
     ```
     { id, agentUserId, listingId|null, contactId, leadId|null, status, startAt, endAt, confirmedAt|null,
       reminder24HSentAt|null, reminder1HSentAt|null, createdAt, updatedAt }
     ```
     Note the exact key `reminder24HSentAt` (capital H).
   - **GAP-02**: no contact details on the row; agents cannot open `GET /portal/contacts/{id}` (needs `lead:view_all`). Show the linked lead (`GET /portal/leads/{leadId}` embeds the contact — agents can read their own leads) as the workaround: resolve `leadId` → `LeadDetailOut.contact`.
   - Row: time (local tz + agency tz label), status badge, listing (resolve `listingId`), agent (team leads/admins), contact (via lead), reminder stamps as icons.
   - Actions by status: `requested` → Confirm / Cancel; `confirmed` → Complete / No-show / Cancel. `POST /portal/appointments/{appointment_id}/status {toStatus}` → `AppointmentOut`. Confirm dialog for cancel/no-show. Detail: `GET /portal/appointments/{appointment_id}`.
   - Scope: agent → own tours; team lead → team; marketing/admin → all.

2. **Availability** (`/portal/agents/[profileId]/availability`, linked from My profile) — own profile or `agent:manage`
   - `GET /portal/agents/{profile_id}/availability` → `AvailabilityRuleOut[]` `{id, dayOfWeek: 0–6|null (Monday = 0), date: YYYY-MM-DD|null, startTime: "HH:MM:SS", endTime, isBlock}`.
   - Editor: a weekly grid (one row per `dayOfWeek` window) plus a list of dated exceptions (`date` set): an open exception **adds** a window on that date; `isBlock: true` **removes** matching time on that date (holiday). Only dated rows may block.
   - Save is a **full replacement**: `PUT /portal/agents/{profile_id}/availability {rules: AvailabilityRuleIn[]}` → the new list. Each rule has exactly one of `dayOfWeek`/`date`, `startTime` strictly before `endTime`, and `isBlock` only on dated rows — each violation is a 422 with `loc` pointing at the rule index. The rule count is capped server-side (422 past the cap).
   - Times are interpreted in `settings.appointments.timezone` (default UTC); slot length and buffer come from `settings.appointments.slot_minutes` / `buffer_minutes` (defaults 60/0) — show these read-only so the agent knows how windows become slots. Public slots are computed from window start on a fixed grid, minus existing busy tours ± buffer.

3. **Calendar feed**: `GET /portal/agents/{profile_id}/ical` → `{url}` — a secret-token URL for `GET /appointments/ical/{token}` (`text/calendar`, live tours from yesterday onward, TENTATIVE for requested, CONFIRMED for confirmed). Show "copy link / subscribe in Google/Apple/Outlook"; warn that anyone with the link can read the calendar (there is no revoke — **GAP-25**).

---

## 3.17 Portal — Deals, milestones, documents, commission

**Why it exists**: back-office tracking once a lead converts — a checklist, contract documents in private storage, and commission figures visible to admins only.

### Deal status machine

| From | To |
|---|---|
| `open` | `under_contract`, `closed_won`, `closed_lost` |
| `under_contract` | `open`, `closed_won`, `closed_lost` |
| `closed_won`, `closed_lost` | (terminal → 409) |

`closed_lost` requires `lostReason` (409). Closing sets `closedAt`. Closing fires the `deal.closed` webhook event.

### The commission gate
Admins receive `DealWithCommissionOut` (adds `commissionBasis: "percentage"|"flat"|null`, `commissionRate: Money|null` (3 dp), `commissionAmount: Money|null`) from **every** deal endpoint; non-admins receive `DealOut` with **no commission keys at all**. Detect by `"commissionBasis" in deal`, never by role. `GET/PUT …/commission` return 403 for non-admins.

### Screens

1. **Deals list** (`/portal/deals`) 🔒 `deal:manage` (agent, team_lead, admin — **not marketing**)
   - `GET /portal/deals?status=&cursor=&limit=` → `Page<DealOut | DealWithCommissionOut>`. Columns: title, status badge, price + currency, owner (resolve `ownerUserId`), linked listing/lead/contact chips, commission (admins), `closedAt`. Kanban by status optional.
   - Scope: agent → own (owner); team lead → team; admin → all.

2. **Create / edit** (`/portal/deals/new`, `/portal/deals/[id]`)
   - `POST /portal/deals {title (1–255), listingId?, leadId?, contactId?, ownerUserId? (managers may set another user; agents → 403 "Only a manager can assign a deal to another agent"), price?, currency? ("DZD"), notes? (≤ 5000), seedMilestones? (default true)}` → 201. With `seedMilestones` the server creates the five default steps: Offer accepted, Deposit received, Contract signed, Financing approved, Closing.
   - Link pickers: listing (`GET /portal/listings?q=`), lead (`GET /portal/leads`), contact (from the lead's `contactId`), owner (`GET /users` for admins; team leads: `GET /portal/agents` → `userId`). Bad links → 404 "Linked listing/lead/contact not found"; bad owner → 404 "Assigned user not found".
   - `PATCH /portal/deals/{deal_id} {title?, price?, currency?, ownerUserId?, listingId?, leadId?, contactId?, notes?}`; `GET /portal/deals/{deal_id}`; `DELETE` → 204.
   - Status: `POST /portal/deals/{deal_id}/status {toStatus, lostReason? (≤ 500)}`; confirm dialogs on closing.

3. **Commission panel** (admin only; hide the panel entirely for others)
   - `GET /portal/deals/{deal_id}/commission` → `DealWithCommissionOut`.
   - `PUT /portal/deals/{deal_id}/commission {basis: "percentage"|"flat", rate? (0–100, ≤ 3 dp; used for percentage), amount? (> 0; used for flat)}` → `DealWithCommissionOut`. Percentage derives `commissionAmount = price × rate / 100` server-side (needs `price`); show the derived figure read-only.

4. **Milestones checklist** (on the deal page)
   - `GET /portal/deals/{deal_id}/milestones` → `MilestoneOut[]` `{id, dealId, title, dueDate|null, ownerUserId|null, completedAt|null, position, createdAt, updatedAt}` (bare array; sort by `position`).
   - `POST …/milestones {title, dueDate?, ownerUserId?, position? (≥ 0, default 0)}` → 201; `PATCH …/milestones/{milestone_id} {title?, dueDate?, ownerUserId?, position?, completed?: boolean}` (toggling `completed` sets/clears `completedAt`); `DELETE` → 204.
   - Due milestones on non-closed deals trigger an hourly `milestone_due` notification to the milestone owner (or deal owner); changing `dueDate` re-arms it.

5. **Documents** (on the deal page; private bucket, never public)
   - `GET /portal/deals/{deal_id}/documents` → `DocumentOut[]` `{id, dealId, docType, filename, contentType, sizeBytes|null, sha256|null, status: "pending"|"ready"|"failed", signatureStatus: "none"|"requested"|"signed"|"declined", uploadedBy|null, createdAt, updatedAt}`.
   - Upload: `POST …/documents/uploads {docType (1–60, free text e.g. "contract"), filename (≤ 255), contentType (≤ 120), sizeBytes?}` → 201 `{document, uploadUrl, headers}` → `PUT uploadUrl` with `headers` → `POST …/documents/{document_id}/confirm` → `DocumentOut(status: ready, sizeBytes, sha256)` (synchronous — the server fetches the object and hashes it). Errors: 409 "already 'ready'"; 409 "No uploaded file was found" (the PUT did not land — retry the PUT then confirm).
   - Download: `GET …/documents/{document_id}/download` → `{url}` (15 min). 409 if not ready. Delete → 204.
   - `signatureStatus` is always `none` today (**GAP-26**: no e-signature provider); render it as a read-only badge, no action.

---

## 3.18 Portal — Content CMS & blog

**Why it exists**: marketing owns the site's editorial content: block-based pages, versioned legal pages, neighbourhood guides with a boundary, market reports with a generated PDF, and the blog. All routes 🔒 `content:manage` (marketing, admin).

### Screens

1. **Pages** (`/portal/content/pages`, `/portal/content/pages/[id]`)
   - List: `GET /portal/content/pages?cursor=&limit=` → `Page<PageOut>` `{id, slug, title: I18n, blocks: object[], seoMeta: object, status: "draft"|"published", publishedAt|null, createdAt, updatedAt}`.
   - Create/edit: `POST /portal/content/pages {slug (1–160, kebab), title: I18n (required), blocks?: [{type, data?}], seoMeta?: {title?: I18n, description?: I18n, ogImage? (URL ≤ 500)}, status?}` → 201; `PATCH …/{page_id}` (all optional); `GET …/{page_id}`; `DELETE` → 204.
   - Block editor: a sortable list of blocks; `type` ∈ `hero, richtext, listings_grid, cta, image, gallery, faq, stats, contact` (an unknown type is 422). `data` is opaque to the backend — the frontend's shared `blocks/` package defines each block's fields and its public renderer. Image/gallery blocks take **URLs** (no upload pipeline for blocks — **GAP-27**; use an external image URL or a listing photo URL).
   - Publish/unpublish: `POST …/{page_id}/publish` / `…/unpublish` → `PageOut`. `publishedAt` is stamped on first publish and never reset. Slug conflict → 409.
   - Preview: `POST …/{page_id}/preview-token` → `{token}` → open `/preview/{slug}?token=` (public route, works for drafts, shareable without auth).
   - The public site caches pages for 5 min server-side; publishing invalidates it.

2. **Legal pages** (`/portal/content/legal`)
   - `GET /portal/content/legal` → `LegalPageOut[]` (the current version per kind) `{id, kind, version, body: I18n, effectiveAt, isCurrent, createdAt}`.
   - Every edit is a **new version**: `POST /portal/content/legal {kind: "privacy"|"terms"|"fair_treatment"|"license_disclosure", body: I18n, effectiveAt?}` → 201 the new current version (previous flips `isCurrent: false`). There is no PATCH by design (consent proof references a version).
   - History: `GET /portal/content/legal/{kind}/history` → `LegalPageOut[]` (all versions). Show a diff-friendly list.

3. **Neighbourhood guides** (`/portal/content/guides`, `…/[id]`)
   - `GET /portal/content/guides` → `Page<GuideOut>` `{id, slug, name: I18n, body: I18n, boundary: rings|null, seoMeta, status, stats: object, statsComputedAt|null, publishedAt|null, createdAt, updatedAt}`.
   - `POST /portal/content/guides {slug, name: I18n, body?: I18n, boundary?: [[[lon,lat],…],…], seoMeta?, status?}`; `PATCH`; `GET`; `DELETE`; `POST …/publish` / `…/unpublish`.
   - Boundary editor: draw polygons on a map → rings of `[lon, lat]`; bad geometry → 422. Listings inside the boundary are linked live (no save needed). `stats` (`listingCount`, `medianPrice`) are recomputed nightly for published guides with a boundary — show `statsComputedAt` and "computed nightly".

4. **Market reports** (`/portal/content/reports`, `…/[id]`)
   - `GET /portal/content/reports` → `Page<ReportOut>` `{id, slug, title: I18n, stats: object, status: "draft"|"published"|"ready", generatedAt|null, publishedAt|null, createdAt, updatedAt}`.
   - `POST /portal/content/reports {slug, title: I18n, stats?: object}`; `PATCH {slug?, title?, stats?}`; `GET`; `DELETE`.
   - Stats editor: a key/value (or JSON) editor — `stats` is the author-compiled figures rendered into the PDF table and shown publicly; the frontend defines the shape (suggest `{[label]: value}` plus optional series).
   - Publish: `POST …/{report_id}/publish` → `ReportOut(status: published)`; the PDF renders in a worker and the row flips to `ready` (`generatedAt` set). Poll `GET …/{report_id}` every 3 s until `ready` (public download 409s until then). `…/unpublish` → back to `draft`. Editing `stats` after publish does **not** regenerate the PDF (**GAP-28**: unpublish then publish again to re-render).

5. **Blog** (`/portal/blog/posts`, `…/[id]`, `/portal/blog/categories`)
   - Categories: `GET /portal/blog/categories` → `CategoryOut[]` `{id, slug, name: I18n, createdAt, updatedAt}` (bare array); `POST {slug (1–160), name: I18n}` → 201; `GET/PATCH/DELETE …/{category_id}`. Deleting a category leaves its posts uncategorised.
   - Posts: `GET /portal/blog/posts?status=&category_id=&cursor=&limit=` → `Page<PostOut>` `{id, slug, title: I18n, excerpt: I18n|null, body: I18n, categoryId|null, tags: string[], coverImage|null, seoMeta, status: "draft"|"scheduled"|"published", scheduledAt|null, publishedAt|null, createdAt, updatedAt}`.
   - `POST /portal/blog/posts {slug, title: I18n, excerpt?: I18n, body: I18n (HTML), categoryId?, tags?: string[] (lowercased server-side), coverImage? (URL ≤ 500), seoMeta?, status?, scheduledAt?}`; `PATCH` (same, all optional); `GET`; `DELETE`; `POST …/publish` / `…/unpublish`.
   - Scheduling: `status: "scheduled"` requires a **future** `scheduledAt` (422 on create/PATCH with status; 409 when a PATCH leaves a scheduled post with a past/missing time). A sweep publishes due posts within ~5 min; the list should refetch to reflect it. Manual publish sets `publishedAt` once.
   - Body editor: the rich-text component (§2.10) — the server sanitises to the allowlist; excerpt is stored as plain text (tags stripped); when omitted the public excerpt is a truncated body.
   - Unknown `categoryId` → 404; slug conflict → 409.

### Endpoint reference (all 🔒 `content:manage`)

| Resource | List | Create | Get / Patch / Delete | Actions |
|---|---|---|---|---|
| Pages | `GET /portal/content/pages` | `POST /portal/content/pages` | `…/pages/{page_id}` | `…/publish`, `…/unpublish`, `…/preview-token` |
| Legal | `GET /portal/content/legal` | `POST /portal/content/legal` | – | `GET …/legal/{kind}/history` |
| Guides | `GET /portal/content/guides` | `POST /portal/content/guides` | `…/guides/{guide_id}` | `…/publish`, `…/unpublish` |
| Reports | `GET /portal/content/reports` | `POST /portal/content/reports` | `…/reports/{report_id}` | `…/publish`, `…/unpublish` |
| Blog categories | `GET /portal/blog/categories` | `POST /portal/blog/categories` | `…/categories/{category_id}` | – |
| Blog posts | `GET /portal/blog/posts` | `POST /portal/blog/posts` | `…/posts/{post_id}` | `…/publish`, `…/unpublish` |

---

## 3.19 Portal — Review moderation

**Why it exists**: every public review lands `pending`; marketing/admin approve or reject before anything is visible. 🔒 `review:moderate`.

### Screen: Review queue (`/portal/reviews`)
- `GET /portal/reviews?status=&agent_user_id=&cursor=&limit=` → `Page<ReviewOut>`:
  ```
  { id, agentUserId|null, listingId|null, rating, title|null, body, authorName, authorEmail|null, status: "pending"|"approved"|"rejected",
    isVerified, moderatedBy|null, moderatedAt|null, moderationNote|null, createdAt, updatedAt }
  ```
- Tabs: Pending (default), Approved, Rejected. Filter by agent (`agent_user_id`; picker from `GET /portal/agents` → `userId`). `agentUserId` null = agency-wide testimonial.
- Row: rating stars, author, excerpt, target (agent name via roster / "Agency"), listing reference (resolve `listingId`), age. Detail drawer: `GET /portal/reviews/{review_id}`.
- Moderate: `POST /portal/reviews/{review_id}/moderate {status: "approved"|"rejected", isVerified?, note? (≤ 500)}` → `ReviewOut`. `pending` is not a valid target (422). Re-applying the same decision is a 200 no-op; flipping approved↔rejected is a **409** — explain "decisions are final; delete and ask for a resubmission instead".
- Delete: `DELETE /portal/reviews/{review_id}` → 204 (confirm).
- Approved reviews immediately count in the public aggregates and feeds.

---

## 3.20 Portal — Analytics

**Why it exists**: management dashboards from nightly rollups (never raw events) and a per-listing performance report scoped to the viewer's own inventory.

### Screens

1. **Dashboards** (`/portal/analytics`) 🔒 `analytics:view` (team_lead, marketing, admin)
   - Common params: `start`, `end` (`YYYY-MM-DD`; default last 30 days; max window 366 days → 422 beyond). Date-range picker with presets.
   - Traffic: `GET /portal/analytics/traffic` → `{totalViews, totalSaves, totalInquiries, series: [{day, views, saves, inquiries}]}` — three totals + a daily line chart.
   - Top listings: `GET /portal/analytics/top-listings?limit=` (1–100, default 10) → `[{listingId, views, saves, inquiries}]` — resolve titles via `GET /portal/listings/{id}` (**GAP-09**: no batch lookup; cap at 10 and cache).
   - Lead funnel: `GET /portal/analytics/lead-funnel` → `{totalCreated, totalWon, totalLost, conversionRate, series: [{day, leadsCreated, leadsWon, leadsLost}]}` — cohort by creation day (won/lost counted against the day the lead was created).
   - Sources: `GET /portal/analytics/sources` → `[{source, leadsCreated, leadsWon, conversionRate}]` — bar chart / table.
   - Freshness: rollups run at 02:00 UTC and re-aggregate yesterday + today; show "updated nightly".

2. **My listings performance** (`/portal/analytics/listings`) — any portal role (bearer only)
   - `GET /portal/analytics/listing-performance?start=&end=` → `{windowStart, windowEnd, listings: [{listingId, views, saves, inquiries}]}` scoped to the viewer (agent → own, team lead → team, admin → all). A user with no listings gets an empty 200. Resolve titles as above; also link each row to the listing editor.

---

## 3.21 Portal — Users (tenant staff accounts)

**Why it exists**: admins create and manage agency accounts; everyone edits their own profile (§3.2). 🔒 `user:view` / `user:manage` (admin only).

### Screen: Users (`/portal/users`)
- `GET /users?cursor=&limit=` → `Page<UserOut>` `{id, email, role, status: "active"|"disabled", firstName, lastName, locale, phone, emailVerifiedAt, lastLoginAt, createdAt}`. No role/search filter (**GAP-29**) — filter client-side over pages.
- Invite/create: `POST /users {email, password (8–128), role (tenant roles only: buyer_renter, seller, agent, team_lead, admin, marketing — a platform role → 409 "role does not match the account scope"), firstName?, lastName?, locale? ("fr"), phone?}` → 201 `UserOut`. 409 duplicate email; 422 `breached-password`. There is no invitation email: the admin sets a temporary password and shares it (**GAP-30**), so show "ask the user to reset their password on first login".
- Edit: `PATCH /users/{user_id} {role?, status? ("active"|"disabled"), firstName?, lastName?, locale?, phone?}` → `UserOut`. Disabling or demoting revokes the user's live tokens immediately. Confirm before disabling.
- Detail: `GET /users/{user_id}`; delete (soft): `DELETE /users/{user_id}` → 204 (confirm; the account's tokens die immediately).
- Agent accounts also need an agent profile to appear publicly — link to "Create profile" (`POST /portal/agents {userId}`) from the user row.

---

## 3.22 Portal — Syndication

**Why it exists**: pushing published listings to external property portals, with per-listing sync state, a circuit breaker, and manual re-push. 🔒 `listing:manage` (every portal role, including agents — see §6 for the concern this raises).

### Screens

1. **Settings** (`/portal/settings/syndication`)
   - `GET /portal/syndication/settings` → `{portals: [{key, enabled, baseUrl|null, hasApiKey}]}` — one row per portal the backend ships an adapter for. Today the only key is the mock portal (**GAP-31**: no real portal adapter exists; the key is the adapter's constant `MOCK_PORTAL_KEY`; read it from this response, never hardcode).
   - `PUT /portal/syndication/settings {portals: {[key]: {enabled?, baseUrl?, apiKey?}}}` → same shape. Full replacement of the namespace, but an omitted `apiKey` **keeps** the stored one (the key is write-only; `hasApiKey` tells you one exists). Unknown key → 422. Render the API-key field as "set / replace" without echoing.

2. **Sync state** (`/portal/settings/syndication/state`, and a "Syndication" tab on the listing editor)
   - `GET /portal/syndication/state?portal=&cursor=&limit=` → `Page<PortalSyncStateOut>` `{id, listingId, portalKey, remoteId|null, lastStatus: "pending"|"synced"|"removed"|"failed"|"paused", lastPushedAt|null, lastError|null, retryCount, consecutiveFailures, circuitOpen, updatedAt}`.
   - Per listing: `GET /portal/syndication/listings/{listing_id}/state` → `PortalSyncStateOut[]`.
   - Show `circuitOpen` as "paused after repeated failures" with the last error; `paused` rows need a manual re-push.
   - Re-push: `POST /portal/syndication/listings/{listing_id}/repush` → `{[portalKey]: string[]}` (the actions enqueued per portal, e.g. `["push"]`). Resets an open circuit. Errors: 404 when the listing is not a currently-published listing in scope; 409 "no portals enabled".
   - Syncs happen automatically on publish/edit/unpublish; the state list is eventually consistent — refetch on focus.

---

## 3.23 Portal — Webhooks

**Why it exists**: outbound integrations — the agency registers URLs that receive signed POSTs on domain events. 🔒 `webhook:manage` (admin only).

### Screens

1. **Endpoints** (`/portal/settings/webhooks`)
   - `GET /portal/webhooks/endpoints` → `WebhookEndpointOut[]` `{id, url, events: string[], description|null, isActive, circuitOpen, lastError|null, lastDeliveredAt|null, createdAt}` (bare array).
   - Create: `POST /portal/webhooks/endpoints {url (≤ 2000, http(s), public host), events: ≥ 1 of "lead.created" | "listing.published" | "deal.closed", description? (≤ 200)}` → 201 `WebhookEndpointCreatedOut` = the above + **`secret`** (shown exactly once — render a copy box and a warning; it is never returned again). Errors: 422 `invalid-webhook-url` (non-http, unresolvable, private/loopback/link-local address) with a specific `detail`; 422 unknown event.
   - Edit: `PATCH …/{endpoint_id} {url?, events?, description?, isActive?}`; setting `isActive: true` also clears an open circuit. Delete → 204.
   - Show `circuitOpen` as "paused after 5 consecutive failures — re-enable to resume".
   - Document for integrators (a help panel): each delivery is a POST with header `X-Webhook-Event: <event>` and a Stripe-style signature header `t=<unix>,v1=<hmac-sha256 hex>` computed over the body with the secret; the `deal.closed` payload carries an `outcome` field (`closed_won`/`closed_lost`) and never includes commission figures.

2. **Delivery log** (`/portal/settings/webhooks/deliveries`)
   - `GET /portal/webhooks/deliveries?endpoint_id=&cursor=&limit=` → `Page<WebhookDeliveryOut>` `{id, endpointId, eventType, status ("pending"|"delivered"|"failed"), attempts, responseStatus|null, lastError|null, deliveredAt|null, createdAt}`. There is no retry action (**GAP-32**: re-enable the endpoint is the only recovery).

---

## 3.24 Portal — Compliance

**Why it exists**: the cookie banner configuration and the tenant-scoped audit trail. 🔒 `compliance:manage` (admin only).

### Screens

1. **Cookie banner** (`/portal/settings/compliance/cookies`)
   - `GET /portal/compliance/cookie-config` → `{categories: object[], bannerCopy: object, isEnabled} | null`.
   - `PUT /portal/compliance/cookie-config {categories?: object[], bannerCopy?: object, isEnabled?: true}` → same. Full replacement. The shapes of `categories` and `bannerCopy` are frontend-defined (§3.9) — use the same types as the public banner; the backend only checks the envelope.
   - Preview the banner inline from the draft config.

2. **Audit log** (`/portal/settings/compliance/audit`)
   - `GET /portal/compliance/audit-log?action=&cursor=&limit=` → `Page<AuditLogOut>` `{id, tenantId|null, actorUserId|null, actorRole|null, action, target|null, metadata: object, ip|null, createdAt}` — pinned to this tenant. Filter by `action` (free text, exact match; **GAP-33**: the set of action names is not exposed — collect the distinct values from the loaded pages for a dropdown). Row: time, actor, action, target, metadata expandable.
   - Data-subject requests and per-subject exports are self-service under `/me` (§3.10); nothing to do here.

---

## 3.25 Platform back-office

**Why it exists**: the operator's console — onboarding agencies, domains and certificates, plans and billing, suspension/offboarding, impersonation for support, platform staff, cross-tenant metrics and the global audit log. Served on the **bare host** (`/api/v1/platform/*` and `/api/v1/billing/*` are tenant-exempt); the console origin must be in the backend's `CORS_ORIGINS`. 🛡

### Auth
Mirrors §3.2 with its own routes and cookie path (`/api/v1/platform/auth`): `POST /platform/auth/login` → `TokenOut | MfaRequiredOut`; `POST /platform/auth/mfa/verify`; `POST /platform/auth/refresh`; `POST /platform/auth/logout`. Platform users have `tenantId: null` and role `platform_admin` or `platform_support`. MFA enrol/status/disable and sessions use the **shared** `/auth/mfa/*` and `/auth/sessions` routes with the platform bearer token on the bare host (**Assumption**: those routes are not tenant-exempt by prefix but the bare host resolves no tenant; verify against a running instance before relying on it — see §6 A-3). No self-registration; no password-reset flow for platform staff (**GAP-34**).

### Screens

1. **Tenants** (`/platform/tenants`) 🔒 `platform:tenant:view`
   - `GET /platform/tenants?cursor=&limit=` → `Page<TenantOut>`:
     ```
     { id, name, slug, status: "trial"|"active"|"suspended", plan, trialEndsAt|null, offboardingAt|null, deletionScheduledAt|null,
       settings: object, createdAt, updatedAt,
       domains: [{id, domain, isPrimary, verificationStatus: "pending"|"verified"|string, verificationToken|null, verifiedAt|null, createdAt}] }
     ```
   - No search/filter (**GAP-35**). Columns: name, slug, primary domain, status, plan, trial end, offboarding badge.
   - Create 🔒 `platform:tenant:manage`: `POST /platform/tenants {name (1–120), slug (2–63, ^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$), domain (primary), settings?}` → 201 `TenantOut` (status `trial`, `trialEndsAt` = +14 days, a verification token minted for the domain). 409 slug/domain taken.

2. **Tenant detail** (`/platform/tenants/[id]`)
   - `GET /platform/tenants/{tenant_id}` → `TenantOut`. `PATCH {name?, settings?}` (manage) — `settings` is the **whole** free-form blob and a PATCH **replaces** it with the object you send (verified in the service): always load the current blob, merge the edit, and send the full result. Provide a JSON editor with the namespaces from Appendix D documented beside it.
   - Lifecycle (manage): `POST …/suspend`, `POST …/activate` → `TenantOut`. Suspended tenants 402 on every tenant route.
   - Plan (manage): `PUT …/plan {plan}` → `TenantOut`; plans `trial` (25 listings, 3 agents, 1 GB, 500 emails/mo), `starter` (100, 10, 10 GB, 5 000), `growth` (1 000, 50, 100 GB, 50 000), `enterprise` (unlimited). Unknown → 409 (**GAP-36**: the plans table is not exposed; hardcode these four).
   - Billing: `GET …/subscription` → `SubscriptionOut | null` `{id, provider, plan, status, currentPeriodEnd|null, graceUntil|null, cancelAtPeriodEnd}`; `POST …/checkout {plan (1–40), customerEmail}` (manage, send `Idempotency-Key`) → 201 `{url, sessionId}` — open `url` (the stub provider returns a fake URL; a real provider redirects to hosted checkout). Webhooks from the provider drive activation/renewal/dunning without UI involvement.
   - Offboarding (manage): `POST …/offboard` → suspends now, exports the tenant's data to private storage, schedules deletion in 30 days (`deletionScheduledAt`). `POST …/offboard/cancel` → reactivates before the purge. Both need a typed-confirmation dialog.

3. **Domains** (section of tenant detail; manage)
   - `POST /platform/tenants/{tenant_id}/domains {domain, isPrimary?}` → 201 `TenantOut`; `DELETE …/domains/{domain_id}` → `TenantOut` (primary-domain rules enforced → 409).
   - Verification: show the TXT challenge from `verificationToken` with copy-paste instructions ("add a TXT record with this value"), then `POST …/domains/{domain_id}/verify` → `{domain, verificationStatus, verifiedAt|null}`. A daily sweep also re-checks pending domains. TLS certificates are issued on demand for any registered domain (verification gates certificate issuance at the edge).

4. **Impersonate** (manage): `POST /platform/tenants/{tenant_id}/impersonate` → `{accessToken, tokenType, expiresIn (900), impersonation: true, tenantId, tenantSlug, actingAsUserId}`. Audit-logged. The token is a **tenant** token for the tenant's first admin and is only valid on that tenant's host: open a new tab at `https://<primary domain>/portal?impersonation=1` and hand the token over (e.g. via `postMessage` to the same-vendor frontend, or a one-time URL fragment consumed on load and never stored). No refresh — the session ends at `expiresIn`. 409 when the tenant is suspended. Only the first admin can be impersonated (**GAP-37**).

5. **Platform staff** (`/platform/staff`) 🔒 `platform:staff:manage`
   - `GET /platform/staff?cursor=&limit=` → `Page<UserOut>`; `POST /platform/staff {email, password, role: "platform_admin"|"platform_support", firstName?, lastName?}` → 201. No update/disable/delete (**GAP-38**).

6. **Metrics** (`/platform`) 🔒 `platform:tenant:view`
   - `GET /platform/metrics` → `{totalTenants, activeTenants, trialTenants, suspendedTenants, totalListings, totalAgents, tenants: [{tenantId, tenantName, status, plan, listingsCount, agentsCount, storageBytes}]}` — headline tiles + a per-tenant table (sortable client-side).

7. **Audit log** (`/platform/audit`) 🔒 `platform:tenant:view`
   - `GET /platform/audit-log?tenant_id=&action=&cursor=&limit=` → `Page<AuditLogOut>` (cross-tenant; same row shape as §3.24).

### Endpoint reference

| Method & path | Perm |
|---|---|
| `POST /platform/auth/login` · `…/mfa/verify` · `…/refresh` · `…/logout` | 🌐 / cookie / bearer |
| `GET /platform/tenants` · `GET /platform/tenants/{tenant_id}` · `GET …/subscription` | `platform:tenant:view` |
| `POST /platform/tenants` · `PATCH …/{tenant_id}` · `…/suspend` · `…/activate` · `PUT …/plan` · `…/offboard` · `…/offboard/cancel` · `…/impersonate` · `…/domains` · `DELETE …/domains/{domain_id}` · `…/domains/{domain_id}/verify` · `…/checkout` | `platform:tenant:manage` |
| `GET /platform/metrics` · `GET /platform/audit-log` | `platform:tenant:view` |
| `GET/POST /platform/staff` | `platform:staff:manage` |
| `POST /billing/webhook` | provider signature only — never called by the frontend |

---

# 4. Cross-module flows

**F1 — Visitor → lead → agent (speed-to-lead).** Public form (§3.4/§3.5/§3.6/§3.7/§3.8) → `POST` capture → server dedupes the contact, scores, assigns (§3.14 rule), seeds a drip, and stages a `lead.created` event → the relay notifies the assigned agent (in-app row + email + WebSocket frame `lead_assigned`) → the portal bell (§3.10 component in the portal shell) shows it and the inbox refetches → the agent logs a `call`/`note` (§3.14) which stamps `firstResponseAt` and stops the drip. Unassigned for 30 min → admins get `lead_escalated`. Frontend responsibilities: honeypot fields, UTM capture, the bell + WebSocket, refetch-on-event.

**F2 — Tour booking.** Public slots (§3.5) come from the agent's availability template (§3.16) minus busy tours; booking creates the appointment **and** a `tour_request` lead forced to that agent (§3.14); the agent confirms/cancels in the agenda (§3.16) which emails the visitor; reminders go out at 24 h and 1 h automatically; a `no_show` writes a −15 score activity on the lead. The visitor sees the tour in My tours (§3.10) only with a verified email matching the booking email.

**F3 — Listing publish fan-out.** Editor (§3.12) → `transition → published` → server: alerts every matching saved search (§3.8/§3.10, `instant` frequency emails now, digests later), enqueues portal syncs (§3.22 state rows appear), emits `listing.published` to webhooks (§3.23), and the listing appears in public search/map/detail (§3.3), sitemap and feeds. The Syndication tab on the editor is where the agent watches the fan-out.

**F4 — Media three-step.** Presign → PUT → confirm → poll (§3.13 for listings, §3.15 for agent photos, §3.17 for deal documents). One shared uploader component; the differences are the endpoints and whether processing is asynchronous (listing media, photos) or synchronous (documents).

**F5 — Lead → deal.** From a lead detail (§3.14) "Create deal" prefills `leadId`, `contactId`, `listingId`, owner = the lead's agent (§3.17). Milestone due dates produce `milestone_due` notifications (§3.10 component). Closing the deal emits `deal.closed` (webhooks) and, when the listing is sold/rented, the agent should transition the listing (§3.12) — the backend does not do it automatically.

**F6 — Consent → analytics → dashboards.** Banner choice (§3.9) → session id sent with beacons only after analytics consent → nightly rollups → portal dashboards (§3.20). Rejected consent still allows fully anonymous (no session id) counting.

**F7 — Anonymous alert double opt-in.** Signup (§3.8) → confirm link → the confirm call activates the search, records marketing consent, and creates a `search_signup` lead visible in the CRM (§3.14). Unsubscribe links in every alert email hit the public unsubscribe route.

**F8 — Impersonation.** Platform console (§3.25) mints a tenant token → the portal (§3.11) detects the `imp` claim and shows the banner → every portal write is audit-logged as the impersonated admin with the staff id attached → the session dies at TTL with no refresh.

**F9 — Erasure.** `DELETE /me` (§3.10) → tokens revoked (every open tab must handle the resulting 401 by signing out) → 30-day purge anonymises CRM contact PII, tombstones the account, deletes favourites/saved searches/notifications. Portal views of that contact (§3.14) show anonymised fields afterwards.

**F10 — Suspension.** Platform suspend/trial-expiry/dunning (§3.25) → every tenant request answers 402 → public site shows maintenance, portal signs out to a maintenance page, WebSocket closes 1008. Reactivation is immediate (the tenant cache is invalidated server-side).

---

# 5. Build order

Each phase is independently demonstrable; dependencies are noted.

| Phase | Scope | Depends on |
|---|---|---|
| **0. Foundation** | API client (Problem parsing, bearer + refresh + one-retry, Idempotency-Key, `X-Request-ID` surfacing), tenant bootstrap (`/site/config`, 404/402 handlers), i18n (ar/fr/en, RTL, locale param), permission helper, the four-state list/data primitives, capture-form scaffold, uploader, status badges | – |
| **1. Public core** | §3.1, §3.3 (search, map, detail, SEO), §3.7 (pages, legal, guides, reports, blog) | 0 |
| **2. Lead generation** | §3.4 (lead form, WhatsApp), §3.5 (directory, profile, booking, reviews), §3.6 (valuation, mortgage), §3.8 (alert signup), §3.9 (consent, beacons) | 1 (listing detail hosts the forms) |
| **3. Auth & buyer account** | §3.2 (all flows incl. MFA, sessions, OAuth buttons), §3.10 (favourites, saved searches, tours, notifications + WebSocket, preferences, privacy) | 0; favourites need 1 |
| **4. Portal core** | §3.11 shell, §3.12 listings, §3.13 media, §3.14 CRM, §3.16 tours & availability | 3 (auth), uploader from 0 |
| **5. Portal people & money** | §3.15 agents & teams, §3.17 deals | 4 |
| **6. Portal marketing & ops** | §3.18 CMS & blog editors (block editor shares the `blocks/` package with phase 1), §3.19 reviews, §3.20 analytics, §3.21 users, §3.22 syndication, §3.23 webhooks, §3.24 compliance | 4; §3.18 renderer from 1 |
| **7. Platform console** | §3.25 | 3 (auth component), 0 |

Raise **GAP-01** and **GAP-02** (contact details on lead and appointment rows) with the backend before starting phase 4 — they shape the two most-used portal screens.

---

# 6. Gaps / Assumptions

Everything a screen needs that the backend does not provide, or that had to be assumed. "Gap" = missing capability; "Assumption" = a reading of the backend that should be confirmed against a running instance.

### Gaps that degrade a core journey

| # | Gap | Where | Suggested backend change |
|---|---|---|---|
| GAP-01 | Lead list rows carry only `contactId`; agents cannot read contacts (`lead:view_all`) | §3.14 | Embed `contact: {firstName, lastName, email, phone}` in `LeadOut` |
| GAP-02 | Appointment rows carry only `contactId`/`leadId` | §3.16 | Embed a contact summary in `AppointmentOut` |
| GAP-03 | No buyer-side tour cancel | §3.10 | `POST /me/appointments/{id}/cancel` (requested/confirmed → cancelled) |
| GAP-04 | Public listing has no agent (id, slug, name, photo) | §3.3 | Add `agent: {slug, displayName, photoVariants} \| null` to the public detail |
| GAP-12 | In-app notifications carry `type` + `payload` only; no rendered title/body | §3.10 | Return `title`/`body` rendered in the user's locale alongside `payload` |
| GAP-13 | All six notification types are staff-facing; buyer notification centre is empty | §3.10 | Add buyer types (tour confirmed/cancelled/reminder for the visitor, saved-search match) |
| GAP-16 | Portal listings list has no `agentId` filter (team leads cannot filter by member) | §3.12 | Add `agent_id` query param |
| GAP-18 | Leads inbox has no keyword search | §3.14 | Add `q` over contact name/email/phone |

### Gaps that are workaroundable

| # | Gap | Where |
|---|---|---|
| GAP-05 | No authenticated consent read/write (`/me/consent`) | §3.9 |
| GAP-07 | `sms`/`whatsapp` notification channels have no adapter (sends logged as skipped) | §3.10 |
| GAP-08 | No contacts list/search endpoint | §3.14 |
| GAP-09 | No batch lookup for listings by ids (analytics tables resolve one by one) | §3.20 |
| GAP-10 | MFA "expected for your role" is not signalled by the API | §3.2 |
| GAP-11 | No signed-in password change | §3.2 |
| GAP-14 | No cancel for a pending erasure | §3.10 |
| GAP-15 | `ListingOut` has no cover image | §3.12 |
| GAP-17 | Media reorder is one PATCH per item | §3.13 |
| GAP-19 | Appointments cannot be filtered by lead/contact/listing | §3.14/§3.16 |
| GAP-20 | Tenant `settings` has no schema; branding shape is frontend-defined | §3.1 |
| GAP-21 | `plan`/`usage`/`limits` are served anonymously on `/site/config` | §3.1 |
| GAP-22 | Agent specialties vocabulary and page block types are not exposed by an endpoint | §3.5, §3.7 |
| GAP-23 | `MyAppointmentOut.agentUserId` has no name/slug | §3.10 |
| GAP-24 | Team leads cannot resolve user names (no `user:view`) | §3.15 |
| GAP-25 | iCal secret URL cannot be revoked/rotated | §3.16 |
| GAP-26 | `signatureStatus` has no provider; always `none` | §3.17 |
| GAP-27 | No upload pipeline for page/guide/blog images (`coverImage`, `ogImage`, image blocks are URLs) | §3.18 |
| GAP-28 | Editing report `stats` after publish does not regenerate the PDF | §3.18 |
| GAP-29 | `GET /users` has no role filter or search | §3.21 |
| GAP-30 | No invitation email for new staff; admin sets a temporary password | §3.21 |
| GAP-31 | Only the mock portal adapter exists | §3.22 |
| GAP-32 | No manual retry for a failed webhook delivery | §3.23 |
| GAP-33 | Audit `action` vocabulary is not exposed | §3.24 |
| GAP-34 | No password-reset flow for platform staff | §3.25 |
| GAP-35 | Tenant list has no search/filter | §3.25 |
| GAP-36 | Plans/limits table is not exposed by an endpoint | §3.25 |
| GAP-37 | Impersonation always targets the tenant's first admin | §3.25 |
| GAP-38 | Platform staff cannot be updated, disabled or deleted via API | §3.25 |

### Assumptions to confirm

| # | Assumption |
|---|---|
| A-1 | `POST /portal/leads/{id}/activities` accepts any `ActivityType` from the client; the UI restricts itself to `note/call/email/sms`. |
| A-2 | Platform staff can use the shared `/auth/mfa/*` and `/auth/sessions` routes on the bare host with a platform token (the routes are not prefix-exempt; behaviour on a host that resolves no tenant should be verified). |
| A-3 | The frontend serves the agency site from a registered tenant domain, so CORS works without operator configuration; the platform console origin is added to `CORS_ORIGINS`. |
| A-4 | `POST /portal/tenants/{id}/impersonate` returns 409 for a suspended tenant (from the build log; not re-verified in the router). |

Verified while writing (no longer assumptions): `PATCH /platform/tenants/{id}` replaces the whole `settings` blob; availability rules are validated with 422 (exactly one of `dayOfWeek`/`date`, `startTime` before `endTime`, blocks only on dated rows); the report download gate has no per-endpoint rate limit; saved-search confirm/unsubscribe return 401 on a bad token.

---

# Appendix A — Enums

| Enum | Values |
|---|---|
| Role | `buyer_renter, seller, agent, team_lead, admin, marketing, platform_admin, platform_support` |
| UserStatus | `active, disabled` |
| ListingStatus | `draft, review, published, reserved, sold, rented, archived` |
| ListingPurpose | `sale, rent, rent_daily` |
| PropertyType | `apartment, house, villa, studio, duplex, land, office, retail, warehouse, garage, farm, building, other` |
| PricePeriod | `month, day` (null for sale) |
| SearchSort (public) | `newest, price_asc, price_desc, area_asc, area_desc` |
| PortalSort | `newest, updated, price_asc, price_desc` |
| MediaKind | `photo, video, tour_3d, floorplan, doc` |
| MediaStatus / PhotoStatus | `pending, processing, ready, failed` |
| LeadSource | `listing_form, valuation, mortgage, market_report, search_signup, chat, whatsapp_click, tour_request, phone, portal, ad, other` |
| LeadStage | `new, contacted, qualified, touring, offer, won, lost` |
| ActivityType | `note, call, email, sms, status_change, assignment, tour, no_show, system` |
| AssignmentStrategy | `listing_agent, round_robin, territory` |
| AppointmentStatus | `requested, confirmed, completed, cancelled, no_show` |
| AlertFrequency | `instant, daily, weekly` |
| NotificationType | `lead_assigned, lead_escalated, appointment_reminder, appointment_confirmed, appointment_cancelled, milestone_due` |
| NotificationChannel | `in_app, email, sms, whatsapp` |
| DealStatus | `open, under_contract, closed_won, closed_lost` |
| CommissionBasis | `percentage, flat` |
| DealDocumentStatus | `pending, ready, failed` |
| SignatureStatus | `none, requested, signed, declined` |
| PageStatus (pages, guides) | `draft, published` |
| BlogPostStatus | `draft, scheduled, published` |
| ReportStatus | `draft, published, ready` |
| LegalKind | `privacy, terms, fair_treatment, license_disclosure` |
| ReviewStatus | `pending, approved, rejected` |
| ConsentCategory | `necessary, analytics, marketing` |
| DsrKind / DsrStatus | `export, erasure` / `pending, completed, cancelled` |
| SyncStatus | `pending, synced, removed, failed, paused` |
| Webhook events | `lead.created, listing.published, deal.closed` |
| TenantStatus | `trial, active, suspended` |
| Plan keys | `trial, starter, growth, enterprise` |
| Agent specialties | `residential_sales, residential_rentals, commercial, luxury, land, new_developments, off_plan, property_management, valuation, industrial` |
| Page block types | `hero, richtext, listings_grid, cta, image, gallery, faq, stats, contact` |
| Locales | `ar, fr, en` (default `fr`) |

# Appendix B — Error catalogue

| Status | slug | When | UI behaviour |
|---|---|---|---|
| 400 | `invalid-cursor` | Malformed cursor, or a cursor minted under another sort | Drop the cursor, refetch page 1 |
| 400 | `invalid-webhook` | Billing webhook signature (never from the frontend) | – |
| 401 | `unauthorized` | Missing/expired/revoked token, wrong credentials, bad reset/verify/MFA token, wrong tenant host | Data calls: refresh once then sign out. Auth forms: show `detail` |
| 402 | `tenant-suspended` | Agency suspended | Global maintenance screen |
| 403 | `permission-denied` | Role lacks the permission or an in-service gate | Toast `detail`; hide the control next time |
| 403 | `quota-exceeded` | Plan limit (listings, agents, storage), file too large, photo quota | Show the plan/usage card with `detail` |
| 404 | `not-found` | Unknown id, out-of-scope row, unpublished public resource, unknown host | "Not found" (never "no access") |
| 409 | `conflict` | Invalid state transition, duplicate slug/email, missing lost reason, slot taken, already confirmed, cap reached, terminal decision flip, WhatsApp not configured | Show `detail` inline; refetch the resource |
| 409 | `idempotency-key-in-flight` | Same Idempotency-Key still executing | Wait, then retry with the same key |
| 422 | `validation-error` | Field validation; `errors[{type, loc, msg}]` | Map `loc` to fields; `renderedAt` errors → refresh the form |
| 422 | `breached-password` | Password appears in a breach corpus | Keep the form; ask for another password |
| 422 | `invalid-webhook-url` | Webhook target is not a public http(s) URL | Inline on the URL field |
| 429 | `rate-limited` | Sliding-window limit; `Retry-After` header + `retry_after` body | Countdown on the submit; no auto-retry |
| 501 | `feature-not-configured` | OAuth provider without credentials | Hide the feature |
| 503 | `upstream-unavailable` | AI provider failed/timed out | "Try again" |
| 500 | `internal-error` | Unexpected | Generic error with `request_id` |

# Appendix C — Rate limits (per tenant + client IP unless noted)

| Endpoint | Limit |
|---|---|
| `POST /leads/capture`, `POST /leads/capture/whatsapp-click` | 5 / min (shared bucket) |
| `POST /agents/{slug}/appointments` | 5 / min |
| `POST /reviews` | 5 / min |
| `POST /saved-searches` | 5 / min |
| `POST /tools/mortgage-estimate/email` | 5 / min |
| `POST /tools/mortgage-estimate` | 60 / min |
| `POST /valuations`, `PATCH /valuations/{token}`, `POST /valuations/{token}/complete` | 15 / hour (shared bucket) |
| `POST /consent` | 30 / min |
| `POST /analytics/events` | 120 / min |
| `POST /reports/{slug}/download` | no per-endpoint limit (global budget only) |
| Auth: login, register, password forgot/reset, mfa verify, oauth start/callback | 10 / min per action (configurable) |
| `POST /auth/refresh` | 30 / min |
| Global, per IP, every route except health probes | 300 / min |

# Appendix D — Tenant settings keys the backend reads

`settings` is free-form JSONB edited by the platform (`PATCH /platform/tenants/{id}`), and one namespace by the portal (`syndication`, via §3.22). The public copy on `/site/config` has the `syndication` namespace removed and any credential-like key redacted.

| Key | Read by | Meaning |
|---|---|---|
| `listings.agent_self_publish` (bool) | Listing publish gate | Agents may publish without a manager |
| `contact.whatsapp_number` (string) | WhatsApp handoff | Fallback number when the listing's agent has none |
| `appointments.timezone` (IANA), `slot_minutes`, `buffer_minutes` | Slots/availability | Booking grid (defaults UTC / 60 / 0) |
| `mortgage.default_annual_rate_percent`, `default_term_years`, `default_down_payment_percent` | Calculator | Defaults when inputs are omitted (6.5 / 25 / 20) |
| `leads.drip_sequence` (list) | Drip sweep | Overrides the default follow-up sequence |
| `media.max_photos_per_listing` (int) | Media upload | Photo quota per listing (default 50) |
| `notifications.quiet_hours` (object) | Notify | Digest window for digest-eligible types (none today) |
| `syndication.<portal>.enabled/base_url/api_key` | Syndication | Portal adapter config (portal-managed) |
| `branding.*`, `theme.*` (frontend-defined) | Frontend only | Logo, colours, footer text — define and document in the frontend repo |

*End of specification.*
