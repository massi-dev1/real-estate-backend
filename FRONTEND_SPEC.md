# Frontend Specification — Real Estate Agency Platform

**Version 1.0 · 2026-08-04**
**Target stack:** Next.js 15 (App Router) + TypeScript
**Backend:** FastAPI multi-tenant API, 182 endpoints, base prefix `/api/v1`

---

## How to read this document

This is the complete build specification for the frontend. It is written so a team can build the entire product without opening the backend source.

Every endpoint, field name, enum value, permission and status code in this document was verified against the running implementation. Where the frontend needs something the backend does not provide, it is marked:

> **GAP —** description, with severity and the suggested backend change.

All gaps are collected in **Appendix D**.

**Conventions used throughout:**

| Notation | Meaning |
|---|---|
| `POST /leads/capture` | Path is relative to `/api/v1` unless stated otherwise |
| `field?` | Optional / nullable |
| **A** / **TL** / **AG** / **MK** / **BR** | Admin / Team Lead / Agent / Marketing / Buyer-Renter |
| 🔒 `permission:name` | Requires this RBAC permission |
| 👤 | Ownership-authorized (no permission; you may only act on your own rows) |

---

## Table of contents

**Part I — Foundations**
1. System context
2. Next.js architecture
3. API client layer
4. Authentication & session
5. Multi-tenancy
6. Internationalisation & RTL
7. Design system
8. SEO
9. Cross-cutting behaviours

**Part II — Public website** (§10)
**Part III — Buyer account** (§11)
**Part IV — Agency portal** (§12)
**Part V — Platform back-office** (§13)

**Appendices**
A. Endpoint index · B. Enums · C. Error catalogue · D. Gap register · E. Permission→UI matrix · F. Build sequence

---

# PART I — FOUNDATIONS

## 1. System context

### 1.1 The four surfaces

One API serves four distinct audiences. They differ in authentication model, not just layout.

| Surface | Audience | Auth | Authorization |
|---|---|---|---|
| **Public site** | Anonymous visitors — buyers, renters, sellers | None | — |
| **Buyer account** (`/me/*`) | Registered end-users | Bearer token | **Ownership** — you act on your own rows only |
| **Agency portal** (`/portal/*`) | Agency staff: agent, team lead, marketing, admin | Bearer token | **RBAC permission + visibility scope** |
| **Platform back-office** (`/platform/*`) | Your own staff | Bearer token (separate cookie path) | **Platform permissions**, tenant-exempt |

A tenant token is useless on platform routes and vice versa — the token's `tid` claim is pinned against the resolved tenant on every request.

### 1.2 What the frontend owns

**The frontend owns:** all rendering, all layout, block-type rendering for CMS pages, map rendering, form UX, optimistic updates, and the honeypot fields on public capture forms.

**The backend owns:** all business rules, all authorization, all validation, workflow legality, pricing/commission math, and every enum. The frontend must never re-implement a rule — if a transition is illegal the API returns 409, and that is the source of truth.

### 1.3 One critical consequence: 404 is overloaded

Every scoped resource returns **404 for both "does not exist" and "exists but is not yours."** This is deliberate — it prevents an existence oracle.

**Do not build UI that treats 404 as "never existed."** Never show "this listing was deleted" on a 404. Show "not found or you do not have access."

---

## 2. Next.js architecture

### 2.1 Route groups

```
app/
├─ [locale]/                       # ar | fr | en
│  ├─ (public)/                    # SSR/ISR, no auth
│  │  ├─ page.tsx                          → /
│  │  ├─ listings/page.tsx                 → /listings
│  │  ├─ listings/map/page.tsx             → /listings/map
│  │  ├─ listings/[ref]/page.tsx           → /listings/AGE-2026-00001
│  │  ├─ agents/page.tsx                   → /agents
│  │  ├─ agents/[slug]/page.tsx            → /agents/sam-the-agent
│  │  ├─ agents/[slug]/book/page.tsx       → tour booking
│  │  ├─ estimate/page.tsx                 → valuation wizard
│  │  ├─ tools/mortgage/page.tsx
│  │  ├─ blog/…  guides/…  reports/…  legal/…
│  │  └─ [slug]/page.tsx                   → CMS page (catch-all, last)
│  ├─ (account)/                   # client-heavy, auth required
│  │  └─ account/{favorites,searches,tours,notifications,settings,privacy}
│  ├─ (portal)/                    # SPA-like, auth + RBAC
│  │  └─ portal/{listings,leads,contacts,agents,teams,tours,deals,content,blog,reviews,analytics,syndication,webhooks,users,compliance}
│  └─ (platform)/                  # platform staff only
│     └─ platform/{tenants,metrics,audit,staff}
├─ api/                            # BFF routes (token refresh proxy only)
└─ middleware.ts                   # locale + tenant resolution
```

**Catch-all ordering matters.** `[slug]/page.tsx` for CMS pages must be the *last* segment in `(public)`; Next.js resolves static segments before dynamic ones, so `/listings` will not be swallowed. Verify this on every new public route you add.

### 2.2 Rendering strategy per surface

| Surface | Strategy | Why |
|---|---|---|
| Public listing detail | **ISR**, revalidate 300s + on-demand | Backend already sets `Cache-Control: s-maxage` + `ETag`; SEO-critical |
| Public listing search | **SSR** (dynamic) | Filters are query-driven; must be crawlable and shareable |
| Public map | **Client component** inside an SSR shell | Map libraries are client-only; the shell carries SEO metadata |
| CMS page / blog / guide / legal | **ISR**, revalidate 300s | Content changes rarely; matches the backend's 5-min cache TTL |
| Buyer account | **Client** | Personal, never cached, no SEO value |
| Agency portal | **Client** | Dense interaction, role-gated, no SEO value |
| Platform back-office | **Client** | Same |

**Server components must not hold the access token.** The token lives in browser memory (§4.3). Server-rendered public pages are anonymous by definition, so this is not a conflict — no public page needs an authenticated fetch.

### 2.3 Middleware

`middleware.ts` runs on every request and does two things:

1. **Locale resolution** — if the path has no locale prefix, negotiate from `Accept-Language` against `["ar","fr","en"]` (default `fr`) and rewrite.
2. **Tenant pass-through** — forward the incoming `Host` header on all server-side API calls. The backend resolves the tenant from `Host`; getting this wrong means every request hits the wrong agency or 404s.

```ts
// middleware.ts — locale prefix + tenant host forwarding
import { NextRequest, NextResponse } from "next/server";

const LOCALES = ["ar", "fr", "en"] as const;
const DEFAULT_LOCALE = "fr";

export function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;
  if (pathname.startsWith("/_next") || pathname.startsWith("/api")) {
    return NextResponse.next();
  }
  const hasLocale = LOCALES.some(
    (l) => pathname === `/${l}` || pathname.startsWith(`/${l}/`),
  );
  if (hasLocale) return NextResponse.next();

  const header = req.headers.get("accept-language") ?? "";
  const preferred =
    header
      .split(",")
      .map((p) => p.split(";")[0].trim().slice(0, 2).toLowerCase())
      .find((c) => (LOCALES as readonly string[]).includes(c)) ?? DEFAULT_LOCALE;

  const url = req.nextUrl.clone();
  url.pathname = `/${preferred}${pathname}`;
  return NextResponse.redirect(url);
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
```

### 2.4 Environment

| Variable | Example | Notes |
|---|---|---|
| `API_BASE_URL` | `http://api:8000` | Server-side (container-internal) |
| `NEXT_PUBLIC_API_BASE_URL` | `https://agency-a.com` | Browser-side; **same origin as the site** so the refresh cookie is sent |
| `NEXT_PUBLIC_MAP_TILES_URL` | — | Map tile provider |

**The browser API base must be same-origin with the site.** The refresh token is an `httpOnly` cookie scoped to path `/api/v1/auth`; a cross-origin API host means the cookie is never sent and sessions silently die on refresh.

**Local development against a real tenant:** tenants are resolved by `Host`, so `localhost:3000` resolves to no tenant and returns 404. Add hosts entries (`127.0.0.1 agency-a.test`) and browse `http://agency-a.test:3000`.

---

## 3. API client layer

### 3.1 The contract

- **Wire format is camelCase.** Both directions. No conversion layer needed.
- **Request bodies reject unknown fields** (`extra="forbid"`). Sending a stray field is a 422, not a silent ignore. Never spread a response object back into a request body — strip it to the documented input fields.
- **All errors are RFC 9457 `application/problem+json`.**

### 3.2 Error shape

```ts
type ProblemDetail = {
  type: string;      // "https://api.realestate.example/errors/not-found"
  title: string;
  status: number;
  instance: string;
  detail?: string;
  requestId?: string;
  // validation-error only:
  errors?: { type: string; loc: (string | number)[]; msg: string }[];
  // rate-limited only:
  retryAfter?: number;
};
```

Branch on the **slug** (last path segment of `type`), never on `title` — titles are human copy and may change. Full catalogue in Appendix C.

### 3.3 Client implementation

```ts
export class ApiError extends Error {
  constructor(
    readonly slug: string,
    readonly status: number,
    readonly problem: ProblemDetail,
  ) {
    super(problem.detail ?? problem.title);
  }
}

const slugOf = (type: string) => type.split("/").pop() ?? "unknown";

let accessToken: string | null = null;          // memory only — never localStorage
export const setAccessToken = (t: string | null) => { accessToken = t; };

// Single-flight refresh: N concurrent 401s must trigger exactly one refresh.
let refreshInFlight: Promise<boolean> | null = null;

async function refresh(): Promise<boolean> {
  refreshInFlight ??= (async () => {
    try {
      const res = await fetch(`${BASE}/api/v1/auth/refresh`, {
        method: "POST",
        credentials: "include",          // sends the httpOnly refresh cookie
      });
      if (!res.ok) return false;
      setAccessToken((await res.json()).accessToken);
      return true;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

export async function api<T>(
  path: string,
  init: RequestInit & { idempotencyKey?: string } = {},
): Promise<T> {
  const call = () =>
    fetch(`${BASE}/api/v1${path}`, {
      ...init,
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
        ...(init.idempotencyKey ? { "Idempotency-Key": init.idempotencyKey } : {}),
        ...init.headers,
      },
    });

  let res = await call();

  // One retry after a successful refresh. Never loop.
  if (res.status === 401 && accessToken && (await refresh())) {
    res = await call();
  }

  if (res.status === 204) return undefined as T;
  if (!res.ok) {
    const problem: ProblemDetail = await res.json();
    throw new ApiError(slugOf(problem.type), res.status, problem);
  }
  return res.json();
}
```

**Do not retry a 401 when there was no token to begin with** — that is an anonymous call to a protected route, and retrying only doubles the traffic.

### 3.4 Cursor pagination

Every list endpoint follows one envelope:

```ts
type Page<T> = {
  items: T[];
  nextCursor: string | null;
  totalEstimate: number | null;   // null on most public lists
};
```

- `limit` defaults to **24**, maximum **100**.
- `cursor` is opaque. **Never construct, parse, or persist it across a filter change.**
- A malformed or mismatched cursor is **400 `invalid-cursor`**.
- **Changing any filter or sort resets pagination.** A cursor is minted under a specific sort; replaying it under another sort is a clean 400 by design.

```ts
export function useCursorList<T>(path: string, params: Record<string, unknown>) {
  const [items, setItems] = useState<T[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const key = JSON.stringify(params);

  useEffect(() => { setItems([]); setCursor(null); setDone(false); }, [key]);

  const loadMore = useCallback(async () => {
    const qs = new URLSearchParams(
      Object.entries({ ...params, ...(cursor ? { cursor } : {}) })
        .filter(([, v]) => v !== undefined && v !== null && v !== "")
        .map(([k, v]) => [k, String(v)]),
    );
    const page = await api<Page<T>>(`${path}?${qs}`);
    setItems((prev) => [...prev, ...page.items]);
    setCursor(page.nextCursor);
    setDone(page.nextCursor === null);
  }, [path, key, cursor]);

  return { items, loadMore, done };
}
```

`totalEstimate` is `null` on most public browse endpoints. **Never render "Page 3 of 12"** — the API is keyset-paginated, not offset-paginated. Use "Load more" or an infinite scroll.

### 3.5 Idempotency

Three endpoints honour an `Idempotency-Key` header:

| Endpoint | Why |
|---|---|
| `POST /leads/capture` | A retried submit must not create two leads |
| `POST /agents/{slug}/appointments` | A retried booking must not create two tours |
| `POST /platform/tenants/{id}/checkout` | Money |

Generate a `crypto.randomUUID()` **when the form is first rendered**, not at submit — a key generated at submit time changes on every retry, defeating the purpose. A concurrent duplicate returns **409 `idempotency-key-in-flight`**; treat it as "your first attempt is still processing," not an error.

---

## 4. Authentication & session

### 4.1 Roles

```ts
type Role =
  | "buyer_renter" | "seller"            // self-registerable
  | "agent" | "team_lead" | "marketing" | "admin"   // agency staff, admin-created
  | "platform_admin" | "platform_support";          // your staff
```

Only `buyer_renter` and `seller` can self-register. Requesting any other role at `/auth/register` is a 422.

### 4.2 Login flow

```
POST /auth/login  { email, password }
        │
        ├─ 200 TokenOut          → store accessToken in memory, redirect by role
        ├─ 200 MfaRequiredOut    → MFA challenge screen
        └─ 401 unauthorized      → generic "Invalid email or password"
```

**The 200 response is a union.** Branch on the presence of `mfaRequired`:

```ts
type TokenOut = {
  accessToken: string;
  tokenType: "bearer";
  expiresIn: number;
  user: {
    id: string; tenantId: string | null; email: string;
    role: Role; locale: string;
    emailVerifiedAt: string | null; mfaEnabled: boolean;
  };
};
type MfaRequiredOut = { mfaRequired: true; mfaToken: string; expiresIn: number };

const isMfaChallenge = (r: TokenOut | MfaRequiredOut): r is MfaRequiredOut =>
  "mfaRequired" in r;
```

**MFA challenge:** `POST /auth/mfa/verify { mfaToken, code }` → `TokenOut`.
The `mfaToken` is **single-use and consumed before the code is checked** — a wrong code burns the ticket and the user must re-enter their password. Say so in the error copy: *"Incorrect code. Please sign in again."*

**Critical: never show a specific reason for a failed login.** Locked account, unknown email, wrong password and disabled account all return an identical 401. Always render *"Invalid email or password."* Anything more specific defeats the backend's anti-enumeration design and its account lockout.

### 4.3 Token storage

| Token | Where | Lifetime |
|---|---|---|
| Access | **JavaScript memory** (module variable / context) | ≤15 min |
| Refresh | `httpOnly` cookie, path `/api/v1/auth` | Long-lived, rotated on use |

**Never put the access token in `localStorage` or `sessionStorage`** — any XSS reads it. Memory-only means a page refresh loses it, which is correct: call `POST /auth/refresh` on app mount to restore the session from the cookie.

```ts
// On mount: silent restore. A failure is a normal logged-out state, not an error.
useEffect(() => {
  refresh().then((ok) => setBootstrapped(ok ? "authed" : "anon"));
}, []);
```

**Refresh-token reuse revokes the entire session family.** If two tabs race a refresh, one may be logged out. The single-flight guard in §3.3 prevents this — do not remove it.

### 4.4 Registration

`POST /auth/register` — body: `email`, `password` (8–128), `role?` (`buyer_renter` default), `firstName?`, `lastName?`, `locale?`, `phone?`.

Two error cases need specific copy:

| Slug | Status | Copy |
|---|---|---|
| `breached-password` | 422 | *"This password has appeared in a known data breach. Please choose a different one."* |
| `conflict` | 409 | *"An account with this email already exists."* |

The breach check is privacy-preserving (only a 5-character hash prefix leaves the server) and **fails open** — if the service is unreachable, registration proceeds.

### 4.5 Email verification

Registration sends a verification email. The `TokenOut.user.emailVerifiedAt` field drives a persistent banner while `null`.

`POST /auth/verify-email { token }` → 204. `POST /auth/verify-email/request` → 202, resend.

**Verification is not cosmetic** — `GET /me/appointments` returns an empty list for an unverified account (§11.4). If a user reports missing tours, verification is the first thing to check.

### 4.6 Session & MFA management

| Endpoint | Notes |
|---|---|
| `GET /auth/sessions` | `{id, userAgent?, ip?, createdAt, lastUsedAt?, expiresAt, current}` — one row has `current: true` |
| `DELETE /auth/sessions/{id}` | Revoke one device. 404 for a session that is not yours |
| `GET /auth/mfa/status` | `{enabled, enrolledAt?}` |
| `POST /auth/mfa/enrol` | 201 `{provisioningUri, secret}` — **the only response that ever carries the secret** |
| `POST /auth/mfa/enrol/confirm` | `{code}` → 204. Only now does MFA become active |
| `POST /auth/mfa/disable` | `{password}` → 204 — re-authentication required |

Render `provisioningUri` as a QR code and show `secret` as copyable text for manual entry. **Enrolment is not complete until confirm succeeds** — an abandoned enrolment leaves the account exactly as it was.

### 4.7 OAuth

`GET /auth/oauth/providers` → `{providers: string[]}`. **If the array is empty, render no social buttons at all.** Clicking an unconfigured provider is a 501 `feature-not-configured` — a dead button is worse than no button.

Flow: `POST /auth/oauth/{provider}/start` → `{authorizationUrl, state}` → redirect → callback page posts `POST /auth/oauth/{provider}/callback { code, state }` → `TokenOut`.

### 4.8 Password reset

`POST /auth/password/forgot { email }` → **always 202**, regardless of whether the account exists. Render *"If an account exists for that address, we've sent a reset link."* Never confirm existence.

`POST /auth/password/reset { token, newPassword }` → 204. Can also return `breached-password` (422) — and the reset token is **not consumed** on that failure, so the user can retry with a different password using the same link.

### 4.9 Post-login routing

```ts
const HOME_BY_ROLE: Record<Role, string> = {
  buyer_renter: "/account", seller: "/account",
  agent: "/portal", team_lead: "/portal", marketing: "/portal", admin: "/portal",
  platform_admin: "/platform", platform_support: "/platform",
};
```

Platform staff authenticate at `/platform/auth/login` — a **different endpoint with a different cookie path**. A tenant token cannot access platform routes and vice versa.

---

## 5. Multi-tenancy

### 5.1 Resolution

The tenant is resolved from the `Host` header on every request. One deployment serves every agency; `agency-a.com` and `agency-b.dz` hit the same code and see completely separate data.

**Every server-side fetch must forward the incoming Host header.** Omitting it means no tenant resolves.

```ts
// Server component fetch — forward Host or nothing resolves
import { headers } from "next/headers";

export async function serverApi<T>(path: string): Promise<T> {
  const host = (await headers()).get("host")!;
  const res = await fetch(`${process.env.API_BASE_URL}/api/v1${path}`, {
    headers: { Host: host },
    next: { revalidate: 300 },
  });
  if (!res.ok) throw new ApiError(/* … */);
  return res.json();
}
```

### 5.2 Tenant states

| Backend response | Meaning | Frontend behaviour |
|---|---|---|
| Normal | Active tenant | Render |
| **404** on every route | Host maps to no tenant | Neutral "site not found" page. Do **not** reveal the platform |
| **402 `tenant-suspended`** | Unpaid or offboarding | Full-page maintenance screen. Catch globally |

Handle 402 in a global error boundary, not per-page — it can appear on any request at any moment.

### 5.3 Site bootstrap

`GET /site/config` — anonymous, Redis-cached (5 min). Fetch once per page load in the root layout and provide via context.

```ts
type SiteConfig = {
  name: string;
  slug: string;
  settings: Record<string, unknown>;   // branding etc. — redacted, never credentials
  plan: string;
  usage:  { listingsCount: number; agentsCount: number; storageBytes: number; emailsSent: number };
  limits: { maxListings: number | null; maxAgents: number | null;
            storageGb: number | null; monthlyEmails: number | null };
};
```

`null` in `limits` means **unlimited** (the Enterprise plan) — render "Unlimited", never "0".

`usage` may lag by up to the cache TTL. Do not use it for hard client-side gating; the server enforces quotas at write time and returns 403 `quota-exceeded`.

### 5.4 Tenant settings

`settings` is free-form JSONB the agency controls. Read defensively — every key may be absent:

| Namespace | Used for |
|---|---|
| `branding` / `theme` | Logo, colours (frontend-defined shape) |
| `contact.whatsapp_number` | WhatsApp fallback number |
| `listings.agent_self_publish` | Whether agents may publish without a manager |
| `appointments.timezone` / `slot_minutes` / `buffer_minutes` | Booking grid |
| `mortgage.*` | Calculator defaults |

> **GAP (Low) —** No schema or endpoint defines the branding/theme shape; `settings` is envelope-validated only. The frontend must define and document its own contract, and tolerate any key being missing.

---

## 6. Internationalisation & RTL

### 6.1 Locales

Supported: **`ar`, `fr`, `en`**. Default: **`fr`**. Fallback chain: **fr → en → ar**.

Negotiation: explicit `?locale=` wins, then `Accept-Language`, then default.

### 6.2 Two different i18n shapes

This trips people up. **The same field has different shapes on the public and portal APIs.**

| Surface | Shape | Example |
|---|---|---|
| **Public** | Single negotiated string | `"title": "Bel appartement F3"` |
| **Portal** | Full object, all locales | `"title": {"fr": "Bel appartement F3", "ar": "شقة جميلة"}` |

The public API resolves one locale per field using the fallback chain, so a field never comes back empty if *any* translation exists. **A French page may legitimately show an English or Arabic value** — this is intended, not a bug.

Portal editors must present all three locales per translatable field, with a clear indicator of which are filled. At least one locale is required; the others are optional.

### 6.3 RTL

Arabic requires a full layout flip.

```tsx
<html lang={locale} dir={locale === "ar" ? "rtl" : "ltr"}>
```

**Rules:**
1. **Use logical CSS properties everywhere.** `margin-inline-start`, not `margin-left`. `padding-inline`, `inset-inline-start`, `border-inline-end`.
2. **Mirror directional icons** (back/forward arrows, chevrons, breadcrumb separators) — but **never mirror** logos, media playback controls, or map pins.
3. **Numbers, prices, dates, phone numbers and reference codes stay LTR** even in Arabic. Wrap in `<bdi>` or `direction: ltr; unicode-bidi: isolate`.
4. **Charts do not flip.** Time still runs left→right on an axis. Flip the surrounding labels and legend only.
5. Test every portal table in Arabic — column order flips and unlabelled action columns become ambiguous.

```css
.price { direction: ltr; unicode-bidi: isolate; font-variant-numeric: tabular-nums; }
[dir="rtl"] .icon-directional { transform: scaleX(-1); }
```

### 6.4 Locale on requests

Send `?locale=` explicitly on public content requests rather than relying on `Accept-Language`. It is unambiguous and makes URLs shareable across users with different browser settings.

One exception: `GET /blog/rss.xml` accepts **only** `?locale=` and ignores `Accept-Language` entirely (RSS clients send no such header).

---

## 7. Design system

### 7.1 Required states

Every data-driven view implements four states. Do not ship a view without all four.

| State | Requirement |
|---|---|
| **Loading** | Skeleton matching the final layout. Never a centred spinner on a full page — it causes layout shift |
| **Empty** | Explain why it is empty and what to do. *"No listings match these filters"* + a reset action — never a bare "No data" |
| **Error** | Message from the error catalogue (Appendix C) + a retry action |
| **Partial permission** | Portal views only — hide what the role cannot see; do not render a disabled control the user can never enable |

### 7.2 Status rendering

Status must be readable without colour alone (accessibility, and colour-blind users). Use a shape + label, with colour as reinforcement.

```ts
const LISTING_STATUS = {
  draft:     { label: "Draft",     tone: "neutral" },
  review:    { label: "In review", tone: "info" },
  published: { label: "Published", tone: "success" },
  reserved:  { label: "Reserved",  tone: "warning" },
  sold:      { label: "Sold",      tone: "neutral" },
  rented:    { label: "Rented",    tone: "neutral" },
  archived:  { label: "Archived",  tone: "muted" },
} as const;
```

### 7.3 Money

Prices are **decimal strings** on the wire (`"12500000.00"`), never JavaScript numbers. Parsing to `number` loses precision on large values.

```ts
export function formatMoney(amount: string, currency: string, locale: string) {
  return new Intl.NumberFormat(locale, {
    style: "currency", currency, maximumFractionDigits: 0,
  }).format(Number(amount));      // display only — never round-trip through Number
}
```

Send money back as a **string**, exactly as received. Default currency is `DZD`.

### 7.4 Dates

All timestamps are **UTC ISO 8601**. Appointment times must be displayed in the **agency's** timezone (`settings.appointments.timezone`), not the browser's — an agent in Paris viewing an Algiers agency's calendar must see Algiers time.

### 7.5 Forms

- Server validation is authoritative. Client validation is UX only.
- Map 422 `errors[].loc` to fields: `loc` is `["body", "fieldName"]` → the field is `loc[1]`.
- Disable submit while in flight; never rely on the user not double-clicking.
- Preserve user input on error. Never clear a form because the server rejected it.

---

## 8. SEO

### 8.1 Server-rendered pages

These must be server-rendered with full metadata: home, listing search, listing detail, agent directory, agent profile, all CMS pages, blog index/post, guide index/detail, legal pages, market report landing.

### 8.2 Backend-generated SEO assets

The backend generates these; the frontend must expose them at the site root, proxying through if the API is on a different path.

| Path | Backend endpoint | Content type |
|---|---|---|
| `/sitemap.xml` | `GET /api/v1/sitemap.xml` | `application/xml` |
| `/blog/rss.xml` | `GET /api/v1/blog/rss.xml?locale=` | `application/rss+xml` |

The sitemap already includes published listings, CMS pages, guides and blog posts for the resolved host. **Do not build a second sitemap** — proxy this one.

### 8.3 JSON-LD

`GET /listings/{ref}` returns a ready-made `jsonLd` object (schema.org `RealEstateListing`) on the **detail** response only — it is `null` on list responses.

```tsx
{listing.jsonLd && (
  <script
    type="application/ld+json"
    dangerouslySetInnerHTML={{ __html: JSON.stringify(listing.jsonLd) }}
  />
)}
```

Do not construct this client-side. Use what the API returns.

> **GAP (Low) —** No JSON-LD is provided for agent profiles (`RealEstateAgent`), blog posts (`BlogPosting`), or organisation-level markup. Build these frontend-side or request backend support.

### 8.4 Canonicals and hreflang

Every public page emits a canonical plus `hreflang` alternates for all three locales:

```tsx
export async function generateMetadata({ params }): Promise<Metadata> {
  const { locale, ref } = await params;
  const listing = await serverApi<PublicListing>(`/listings/${ref}?locale=${locale}`);
    // Listings carry no seoTitle/seoDescription — compose from the content.
    // (CMS pages, blog posts and guides DO have seoTitle/seoDescription/ogImage.)
  return {
    title: `${listing.title} — ${listing.address.city ?? ""}`.trim(),
    description: listing.description?.slice(0, 160),
    alternates: {
      canonical: `/${locale}/listings/${listing.referenceCode}`,
      languages: {
        ar: `/ar/listings/${listing.referenceCode}`,
        fr: `/fr/listings/${listing.referenceCode}`,
        en: `/en/listings/${listing.referenceCode}`,
      },
    },
    openGraph: { images: listing.cover ? [listing.cover.variants.gallery.url] : [] },
  };
}
```

**Canonical to the reference code, not the UUID.** Listing detail accepts both, which means two URLs serve identical content — pick the reference code and canonicalise to it consistently, or search engines will see duplicates.

### 8.5 Robots

`noindex` on: all `/account/*`, `/portal/*`, `/platform/*`, page previews (`?token=`), and search results with filters applied beyond page 1.

---

## 9. Cross-cutting behaviours

### 9.1 Honeypot — every public capture form

Every public form that creates data carries two anti-spam fields. **Omitting them is a 422; getting them wrong silently discards the submission.**

| Field | Type | Rule |
|---|---|---|
| `hp` | string | **Must be empty.** A hidden field a bot fills |
| `renderedAt` | ISO datetime | When the form was rendered. Min 3s before submit, max 24h old |

```tsx
export function useCaptureFields() {
  const renderedAt = useRef(new Date().toISOString()).current;
  return {
    renderedAt,
    hiddenFields: (
      <div aria-hidden="true"
           style={{ position: "absolute", left: "-9999px", height: 0, overflow: "hidden" }}>
        <label htmlFor="hp">Leave this field empty</label>
        <input id="hp" name="hp" type="text" tabIndex={-1} autoComplete="off" defaultValue="" />
      </div>
    ),
  };
}
```

**Hide with off-screen positioning, not `display:none` or `type="hidden"`** — sophisticated bots skip those. Always include a `<label>` and `aria-hidden` so screen readers do not announce it.

**The critical consequence:** a bot-detected submission returns a **completely normal success response with a fabricated UUID**. The frontend cannot tell a real submission from a discarded one. Never build "did this actually save?" logic on the response — and never surface the returned id as proof of anything.

Two real validation errors are visible (422): *"form submitted too quickly"* (under 3s) and *"form is stale"* (over 24h — offer a reload).

**Applies to:** lead capture, WhatsApp click, tour booking, review submission, valuation completion, mortgage email, saved-search signup, report download.

### 9.2 Rate limiting

| Endpoint group | Limit |
|---|---|
| Lead capture, WhatsApp click, tour booking, review, saved-search signup, mortgage email | 5 / min |
| Valuation (all three steps, shared) | 15 / hour |
| Mortgage calculate | 60 / min |
| Consent | 30 / min |
| Analytics events | 120 / min |
| Auth (per action) | Configurable; refresh gets 3× |
| Global per-IP | 300 / min |

A 429 carries a `Retry-After` header **and** `retryAfter` in the body. Show a countdown and disable submit — do not auto-retry.

### 9.3 Async work — poll, do not assume

Several operations complete after the response returns. The UI must poll or the user sees a permanently "processing" state.

| Operation | Poll | Terminal states |
|---|---|---|
| Listing media processing | `GET /portal/listings/{id}/media` | `ready` / `failed` |
| Agent photo | `GET /portal/agents/{id}` → `photoStatus` | `ready` / `failed` |
| Market report PDF | `GET /portal/content/reports/{id}` → `status` | `ready` |

Poll every 2s, back off to 5s after 30s, stop at 2 minutes and offer a manual refresh.

### 9.4 Media upload — three steps, always

Identical for listing media, agent photos and deal documents.

```ts
// 1. Presign — the server creates the row and returns a direct-to-storage URL
const { media, uploadUrl, uploadHeaders } = await api<UploadTicket>(
  `/portal/listings/${listingId}/media/uploads`,
  { method: "POST", body: JSON.stringify({ kind: "photo", contentType: file.type, sizeBytes: file.size }) },
);

// 2. PUT the bytes straight to storage — NOT through the API.
//    Send EXACTLY uploadHeaders: any extra header breaks the signature.
await fetch(uploadUrl, { method: "PUT", body: file, headers: uploadHeaders });

// 3. Confirm — flips to processing and enqueues the worker
await api(`/portal/media/${media.id}/confirm`, { method: "POST" });
```

**Do not send the file through the API.** Do not add `Authorization` to the PUT — it is a presigned URL and an extra header invalidates the signature.

Deal documents differ: confirm is **synchronous** (the server computes the SHA-256 inline), so the document is `ready` immediately with no polling.

The presigned URL expires (`expiresInSeconds`). For a large file on a slow connection, re-presign on failure rather than retrying a dead URL.

### 9.5 Quota errors

403 `quota-exceeded` on create actions. Copy must name the limit and the action:

> *"You've reached your plan's limit of 100 listings. Archive an existing listing or upgrade your plan."*

Read current usage from `GET /site/config`. Show a warning at 80%, but **never block client-side** — the server is authoritative and usage data may be stale.

### 9.6 WebSocket notifications

```
POST /me/notifications/ws-ticket  →  { ticket, expiresIn }   (60s TTL, single-use)
        │
WS /api/v1/ws/notifications?ticket=<ticket>
```

**Never put the access token in the WebSocket URL** — URLs leak into logs and proxies. The ticket exists for this reason.

```ts
function connectNotifications(onMessage: (n: Notification) => void) {
  let socket: WebSocket | null = null;
  let attempt = 0;
  let closed = false;

  async function open() {
    if (closed) return;
    const { ticket } = await api<{ ticket: string }>("/me/notifications/ws-ticket", { method: "POST" });
    const proto = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${proto}://${location.host}/api/v1/ws/notifications?ticket=${ticket}`);

    socket.onopen = () => { attempt = 0; };
    socket.onmessage = (e) => onMessage(JSON.parse(e.data));
    socket.onclose = (e) => {
      if (closed || e.code === 1008) return;          // 1008 = bad ticket; do not retry
      const delay = Math.min(1000 * 2 ** attempt++, 30_000);
      setTimeout(open, delay);                        // exponential backoff
    };
  }

  open();
  return () => { closed = true; socket?.close(); };
}
```

**A new ticket is required for every connection attempt** — tickets are single-use. On reconnect, always re-fetch the unread count and list: pub/sub delivery is best-effort and messages sent while disconnected are lost. The database is the source of truth; the socket is an optimisation.

Close code **1008** means an invalid ticket or a suspended tenant. Do not reconnect — re-authenticate.

### 9.7 Optimistic updates

Safe to apply optimistically (idempotent, low-stakes): favourite toggle, notification mark-read.

**Never optimistic:** anything with server-side workflow validation — listing transitions, lead stages, deal status, moderation, bookings. The server may reject with 409 and the UI would have already lied to the user.

### 9.8 HTTP caching

Listing detail, CMS page detail and legal page detail return `ETag` + `Last-Modified` + `Cache-Control: public, s-maxage=N`, with `Vary: Accept-Language, Origin`.

In Next.js server fetches, pass `next: { revalidate: 300 }` and let the framework handle validators. In client fetches, browsers handle `If-None-Match` automatically — do not implement conditional requests manually.

---

# PART II — PUBLIC WEBSITE

All pages in this part are **anonymous**. No token, no permission. The tenant comes from `Host`.

## 10.1 Home — `/`

**Rendering:** ISR, revalidate 300s.

**Sections**
1. Hero with a search entry point (purpose toggle, city, price range) → submits to `/listings`
2. Featured listings — `GET /listings?limit=8` (featured rows lead every sort automatically)
3. Agency introduction — from a CMS page or `settings.branding`
4. Recent blog posts — `GET /blog/posts?limit=3`
5. Agent highlights — `GET /agents?limit=4`
6. Testimonials — `GET /reviews?limit=6` + `GET /reviews/summary`
7. Valuation call-to-action → `/estimate`
8. Footer: legal index from `GET /legal`

**Endpoints:** all above are anonymous GETs. Fetch in parallel in the server component; a failure in any one section should degrade that section only, not the page.

**Empty states:** a brand-new agency has no listings, agents or posts. Every section must render acceptably with zero items — hide the section rather than showing an empty carousel.

---

## 10.2 Listing search — `/listings`

**Rendering:** SSR (dynamic — filters are query params and must be crawlable/shareable).

### Endpoint

`GET /listings` → `Page<PublicListingOut>` (`totalEstimate` is **null**).

### Query parameters — complete

| Param | Type | Constraint |
|---|---|---|
| `purpose` | enum | `sale` `rent` `rent_daily` |
| `propertyType` | enum | 13 values (Appendix B) |
| `priceMin` / `priceMax` | decimal string | > 0, ≤ 999999999999, 2dp. `priceMin ≤ priceMax` |
| `bedsMin` / `bathsMin` | int | 0–100 |
| `areaMin` | decimal string | > 0, ≤ 99999999 |
| `city` | string | ≤ 100, case-insensitive exact match |
| `features` | string[] | From the 18-value vocabulary. **AND semantics** |
| `q` | string | ≤ 200. Full-text, parsed in the negotiated locale |
| `inBbox` | string | `"minLon,minLat,maxLon,maxLat"` |
| `near` | string | `"lon,lat"` |
| `radiusKm` | float | > 0, ≤ 100, default 5.0. **Requires `near`** |
| `inPolygon` | string | `"lon lat,lon lat,…"` ≤ 100 points, auto-closed, ≥ 3 distinct |
| `sort` | enum | `newest` (default) `price_asc` `price_desc` `area_asc` `area_desc` |
| `locale` | string | `ar` `fr` `en` |
| `cursor` / `limit` | string / int | Opaque / 1–100, default 24 |

**At most one of `inBbox`, `near`, `inPolygon`.** Sending two is a 422.

**`city` is an exact match, not a search.** "Alg" will not match "Alger". Drive it from a select populated by known cities, not a free-text input — or use `q` instead.

### Layout

- Filter sidebar (drawer on mobile) — all params above
- Results grid of listing cards
- Sort control + result count
- "Load more" (never numbered pages — `totalEstimate` is null and pagination is keyset)
- Map view toggle → `/listings/map` carrying the same filters

### Card contents

`title`, `price` + `pricePeriod`, `beds`, `baths`, `areaBuilt`, `address.city`, `cover` image with `blurhash` placeholder, `featured` badge, `referenceCode`.

`cover` is `null` when a listing has no photo — render a placeholder, never a broken image.

### States

- **Loading:** skeleton cards matching the final grid
- **Empty:** *"No properties match your filters."* + "Clear filters" action
- **Error:** retry
- **Invalid cursor (400):** reset to page 1 silently — the user changed a filter mid-scroll

### URL state

Every filter belongs in the query string so results are shareable and back/forward works. **Reset `cursor` whenever any filter or the sort changes.**

### SEO

`noindex` when any filter is applied beyond the first page; the unfiltered index page is the canonical entry point.

---

## 10.3 Map search — `/listings/map`

**Rendering:** client component in an SSR shell.

`GET /listings/map` — takes the same filters as `/listings` plus `locale`. **No pagination** — the viewport is the page.

```ts
type MapOut = {
  clustered: boolean;
  pins:     { id: string; lat: number; lng: number; price: string; status: string }[];
  clusters: { lat: number; lng: number; count: number }[];
};
```

**Behaviour:** ≤ 500 matches returns individual `pins`; beyond that the server returns `clusters` (centroid + count) and `clustered: true`. **Both arrays are always present** — branch on `clustered`, and render `pins` when false, `clusters` when true.

- Clicking a cluster zooms in and refetches with the new `inBbox`
- Clicking a pin opens a preview card → links to the detail page
- Refetch on map idle (debounce ~400ms), not on every pan frame
- Results are cached server-side for 60s, so a small pan may return identical data — that is expected

**Cross-linking:** the filter state must survive switching between `/listings` and `/listings/map`. Keep both reading from the same query string.

---

## 10.4 Listing detail — `/listings/[ref]`

**Rendering:** ISR, revalidate 300s. Backend sends `ETag` + `Last-Modified` + `Cache-Control`.

`GET /listings/{refOrId}?locale=` → `PublicListingOut` with `media[]` and `jsonLd` populated (both `null` on list responses).

**Accepts either the reference code or the UUID.** Canonicalise to the reference code (§8.4) or you create duplicate URLs.

### Response — every field

```ts
type PublicListingOut = {
  id: string;
  referenceCode: string;            // "AGE-2026-00001"
  purpose: "sale" | "rent" | "rent_daily";
  propertyType: PropertyType;
  locale: string;                   // which locale actually resolved
  title: string;                    // already a single negotiated string
  description: string | null;
  price: string;                    // decimal string
  currency: string;                 // "DZD"
  pricePeriod: "month" | "day" | null;
  negotiable: boolean;
  beds: number | null;  baths: number | null;
  areaBuilt: string | null;  areaLand: string | null;
  floor: number | null;  floorsTotal: number | null;
  yearBuilt: number | null;
  features: string[];
  address: { line1?: string; line2?: string; city?: string;
             state?: string; postalCode?: string; country?: string };
  location: { lat: number; lng: number } | null;
  publishedAt: string | null;
  featured: boolean;
  cover: PublicMedia | null;
  media: PublicMedia[] | null;      // detail only
  jsonLd: object | null;            // detail only
};

type PublicMedia = {
  id: string;
  kind: "photo" | "floorplan" | "doc" | "video" | "tour_3d";
  variants: Record<string, { url: string; width: number; height: number }>;
  blurhash: string | null;
  position: number;
  alt: string | null;
  isCover: boolean;
  embedUrl: string | null;          // video / tour_3d only
};
```

### Sections

1. **Gallery** — `media` filtered to `kind === "photo"`, ordered by `position`. Use `blurhash` as the placeholder. Variants: `thumb`, `card`, `gallery`, `full` in both webp and jpeg — use `<picture>` with webp first.
2. **Header** — title, price, `pricePeriod` suffix, `negotiable` badge, reference code, `featured` badge
3. **Key facts** — beds, baths, areas, floor, year
4. **Description**
5. **Features** — from the 18-value vocabulary; map to localised labels + icons
6. **Location map** — only when `location` is non-null
7. **Video / 3D tour** — `media` where `kind` is `video` or `tour_3d`; embed `embedUrl` in an iframe (hosts are backend-allowlisted)
8. **Floorplans** — `kind === "floorplan"`
9. **Contact panel** — lead form + WhatsApp button (§10.5, §10.6)
10. **Tour booking** — links to `/agents/{slug}/book` with this listing preselected
11. **Mortgage widget** — prefilled with this listing's price (§10.11)

> **GAP (Medium) —** The public listing detail does **not** include the assigned agent. There is no `agentId`, no agent name, slug or photo. A "contact this agent" panel showing who is responsible cannot be built. The lead form still routes correctly server-side (assignment is automatic), but the visitor cannot see or choose the agent. *Suggested: add an optional `agent: {slug, displayName, photoVariants} | null` to `PublicListingOut` on the detail response.*

> **GAP (Low) —** `documents` (`kind === "doc"`) are private-bucket only and have no public download path, so a public "download the brochure" is not possible. This is deliberate but worth knowing.

### 404

Unpublished or unknown reference → 404. Render "This property is no longer available" with links back to search — the listing may have sold.

---

## 10.5 Lead capture form

Appears on listing detail, agent profile, and a standalone `/contact` page.

`POST /leads/capture` — rate-limited 5/min, **supports `Idempotency-Key`**.

```ts
type LeadCaptureCreate = {
  contact: {
    firstName?: string;  // ≤80
    lastName?: string;   // ≤80
    email?: string;      // ≤320, lowercased server-side
    phone?: string;      // ≤32
    whatsapp?: string;   // ≤32
    marketingConsent: boolean;   // default false
  };
  source: LeadSource;            // required — see below
  listingId?: string;
  message?: string;              // ≤2000
  utmSource?: string; utmMedium?: string; utmCampaign?: string;  // ≤100 each
  page?: string;      // ≤500
  referrer?: string;  // ≤500
  hp: string;         // "" — honeypot
  renderedAt: string; // ISO
};
// → 201 { id: string }
```

**Either `email` or `phone` is required.** Neither → 422. Enforce this client-side too, with a clear message.

`source` for a public form is normally `listing_form`. Full enum in Appendix B.

**Capture UTM parameters and referrer from the browser** — the agency's source-performance analytics depends on it.

**`marketingConsent` must be an unchecked opt-in checkbox.** Never pre-checked, never bundled with the submit action. It feeds the compliance record.

**Success:** show confirmation immediately. Remember §9.1 — a honeypot-tripped submission returns the same 201 with a fake id. Never claim more than "we've received your message."

---

## 10.6 WhatsApp handoff

`POST /leads/capture/whatsapp-click` — same `_CaptureBase` body **without** `source` (fixed server-side). Same 5/min bucket. **No `Idempotency-Key` support** on this one.

```ts
// → 201 { id: string; whatsappUrl: string }
```

**Flow:** the visitor clicks WhatsApp → POST → the lead lands in the CRM *before* they leave → redirect to `whatsappUrl`.

```ts
async function onWhatsAppClick(listingId: string) {
  const { whatsappUrl } = await api<{ whatsappUrl: string }>(
    "/leads/capture/whatsapp-click",
    { method: "POST", body: JSON.stringify({ contact: { phone }, listingId, hp: "", renderedAt }) },
  );
  window.location.href = whatsappUrl;   // server-minted wa.me link, prefilled
}
```

**Never construct the `wa.me` URL client-side.** The server resolves the number (assigned agent → tenant fallback) and prefills the reference code and title.

**409 `conflict`** means no WhatsApp number is configured for that agent or the agency. Hide the button rather than showing an error — but you cannot know in advance, so either attempt-and-hide-on-409, or gate on `settings.contact.whatsapp_number` being present.

Because a contact method is required by the capture schema, collect at least a phone number before the redirect, or the request 422s.

---

## 10.7 Agent directory — `/agents`

`GET /agents?specialty=&cursor=&limit=&locale=` → `Page<PublicAgentOut>` — **`totalEstimate` is populated here** (unlike most public lists).

```ts
type PublicAgentOut = {
  id: string;
  slug: string;
  displayName: string;
  locale: string;
  bio: string | null;
  specialties: string[];
  licenseNo: string | null;
  socials: Record<string, string>;
  photoVariants: Record<string, { url: string; width: number; height: number }>;  // {} until ready
  reviews: { count: number; average: number | null } | null;
};
```

Only **published** profiles appear, and a profile whose account was disabled drops off automatically.

`photoVariants` is an **empty object** until photo processing completes — render initials, not a broken image.

`specialty` filter must come from the 10-value vocabulary (Appendix B); an unknown value is a 422.

---

## 10.8 Agent profile — `/agents/[slug]`

`GET /agents/{slug}?locale=` → `PublicAgentDetailOut` = `PublicAgentOut` + `listings: PublicListingOut[]` (up to 12 published, covers attached).

**Sections:** header (photo, name, specialties, licence, socials), bio, rating summary, their listings, reviews feed, contact form, "Book a tour" CTA.

**Reviews:** `GET /agents/{slug}/reviews?cursor=&limit=` → `Page<PublicReviewOut>`.

```ts
type PublicReviewOut = {
  id: string; agentUserId: string | null;
  rating: number;             // 1–5
  title: string | null; body: string;
  authorName: string; isVerified: boolean; createdAt: string;
};
```

Approved reviews only. No email, no moderation metadata — safe to embed anywhere.

404 for an unpublished or unknown slug.

---

## 10.9 Tour booking — `/agents/[slug]/book`

### Step 1 — pick a date, load slots

`GET /agents/{slug}/slots?date=YYYY-MM-DD` → `{ startAt, endAt }[]`

- **`date` is required.** No date, no slots.
- Bounded to **90 days ahead**; past slots are already excluded.
- An empty array means no availability that day — show "No times available", not an error.
- Times are UTC; **render in the agency timezone** (`settings.appointments.timezone`).

Fetch per selected date. Optionally prefetch the next 7 days to grey out unavailable dates in the picker.

### Step 2 — book

`POST /agents/{slug}/appointments` — rate-limited 5/min, **supports `Idempotency-Key`**.

Body: `_CaptureBase` (contact, listingId?, message?, utm*, hp, renderedAt) **+ `startAt`** (ISO datetime).

```ts
// → 201 { id, status: "requested", startAt, endAt }
```

**`startAt` must exactly equal a slot start returned by step 1.** Anything else is a **409**. Never let the user type a time — always pick from the returned slots, and pass the value back verbatim.

**Race:** two visitors booking the same slot — the server serialises with an advisory lock and the loser gets a 409. Copy: *"That time was just booked. Please choose another."* and **refetch the slots**.

`status` is always `requested`. The agency confirms later; the visitor gets an email. Set expectations in the confirmation screen: *"We've received your request — the agent will confirm shortly."*

---

## 10.10 Valuation wizard — `/estimate`

Three steps held together by a **capability token**. No account needed. All three steps share one 15/hour rate limit.

### Step 1 — address

`POST /valuations` → `201 { token }`

```ts
{ street?: string;      // ≤200
  city: string;         // required, 1–120
  postalCode?: string;  // ≤20
  lat?: number; lng?: number }   // both or neither
```

**Store the token in component state (and optionally `sessionStorage` for resume).** Losing it means starting over — there is no lookup endpoint.

A map pin is the only geo signal (no geocoding), and **`lat`/`lng` materially improve the estimate** — comparables are found by radius. Encourage pin placement.

### Step 2 — property details (partial, repeatable)

`PATCH /valuations/{token}` → `ValuationDraftOut`

```ts
{ propertyType?: PropertyType; areaBuilt?: string;   // >0, ≤99999999.99
  beds?: number;  baths?: number;                    // 0–100
  floor?: number;         // -5..200
  yearBuilt?: number;     // 1800–2100
  condition?: string;     // ≤60
  notes?: string }        // ≤2000
```

Repeatable — safe to call per step in a multi-screen flow. 404 if the row is already completed.

### Step 3 — contact + estimate

`POST /valuations/{token}/complete` — body is `_CaptureBase` only.

```ts
// → 200
type ValuationEstimateOut = {
  id: string;
  estimateLow: string | null;   // null when comparables are insufficient
  estimateHigh: string | null;
  currency: string;
  compsCount: number;
  completedAt: string;
  disclaimer: string;           // render verbatim
};
```

**`estimateLow`/`estimateHigh` are `null` when fewer than 3 comparables were found.** This is a normal outcome, not an error — the lead is still created. Copy: *"We need more local data to give you a range. One of our agents will contact you with a personal valuation."*

**Always render `disclaimer` verbatim** — it is the legal text.

Double-completion → 409.

---

## 10.11 Mortgage calculator — `/tools/mortgage`

### Calculate

`POST /tools/mortgage-estimate` — 60/min, **no honeypot** (a pure calculator, not a lead form).

```ts
// in
{ price: string;                  // required, >0
  downPayment?: string;           // ≥0, must be < price
  annualRatePercent?: number;     // 0–100
  termYears?: number }            // 1–40
// out
{ price, downPayment, loanAmount, annualRatePercent,
  termYears: number, monthlyPayment, totalPaid, totalInterest }   // decimal strings
```

Omitted values fall back to the agency's configured defaults. Safe to call on every input change (debounced ~300ms).

### Email the estimate — this one *is* a lead form

`POST /tools/mortgage-estimate/email` — 5/min. Body = the calculator inputs **+ `_CaptureBase`**; email required unless the honeypot is tripped.

```ts
// → 201 { id, estimate: MortgageEstimateOut }
```

The server **recomputes** — it never trusts a client-supplied figure. An invalid `listingId` is a 404 (validated before the lead insert).

---

## 10.12 CMS page — `/[slug]`

`GET /pages/{slug}?locale=` → `PublicPageOut`. ISR 300s, `ETag`.

```ts
type PublicPageOut = {
  slug: string;
  title: string | null;
  blocks: { type: BlockType; data: Record<string, unknown> }[];
  seoTitle: string | null;
  seoDescription: string | null;
  ogImage: string | null;
};
```

### Block rendering — the frontend owns this entirely

The backend validates only the envelope: `type` must be one of nine allowed values, `data` must be an object. **The inner shape of `data` is frontend-defined.**

```ts
type BlockType =
  | "hero" | "richtext" | "listings_grid" | "cta" | "image"
  | "gallery" | "faq" | "stats" | "contact";
```

```tsx
const BLOCKS: Record<BlockType, React.FC<{ data: any }>> = {
  hero: HeroBlock, richtext: RichTextBlock, listings_grid: ListingsGridBlock,
  cta: CtaBlock, image: ImageBlock, gallery: GalleryBlock,
  faq: FaqBlock, stats: StatsBlock, contact: ContactBlock,
};

export function Blocks({ blocks }: { blocks: PageBlock[] }) {
  return <>{blocks.map((b, i) => {
    const C = BLOCKS[b.type];
    return C ? <C key={i} data={b.data} /> : null;   // unknown type → skip, never crash
  })}</>;
}
```

**Every block renderer must tolerate missing or malformed `data`.** The backend does not validate it, so a content editor can save anything. Never let one bad block break the page.

**You must document your `data` schema per block type** and build the portal's block editor to match — the two are only coupled by convention.

### Preview

`GET /pages/{slug}/preview?token=&locale=` — same shape, works for drafts, no auth. Render with a persistent "Preview — not published" banner and `noindex`.

---

## 10.13 Blog

| Page | Endpoint |
|---|---|
| `/blog` | `GET /blog/posts?category=&tag=&cursor=&limit=&locale=` → `Page<PublicPostOut>` |
| `/blog/[slug]` | `GET /blog/posts/{slug}?locale=` |
| Category / tag filters | Same list endpoint; `GET /blog/categories?locale=` for the nav |
| `/blog/rss.xml` | Proxy `GET /blog/rss.xml?locale=` |

```ts
type PublicPostOut = {
  slug: string; title: string | null;
  excerpt: string | null;      // auto-generated teaser when the author left it blank
  body: string | null;         // sanitized HTML — safe to render
  tags: string[]; coverImage: string | null;
  category: { slug: string; name: string | null } | null;
  publishedAt: string | null;
  seoTitle: string | null; seoDescription: string | null; ogImage: string | null;
};
```

**`body` is server-sanitized HTML** (allowlist: `p br strong em b i u ul ol li h2-h4 blockquote a img`; every link gets `rel="noopener noreferrer nofollow"`; `javascript:` blocked). Render with `dangerouslySetInnerHTML` — this is one of the few safe cases, because sanitisation happened server-side at write time.

`totalEstimate` is null. Scheduled posts appear automatically at their time (a sweep runs every 5 min) — no frontend action needed.

---

## 10.14 Neighborhood guides

| Page | Endpoint |
|---|---|
| `/guides` | `GET /guides?cursor=&limit=&locale=` → `Page<PublicGuideOut>` |
| `/guides/[slug]` | `GET /guides/{slug}?cursor=&limit=&locale=` → `PublicGuideDetailOut` |

```ts
type PublicGuideOut = {
  slug: string; name: string | null; body: string | null;
  boundary: [number, number][][] | null;   // rings of [lon, lat]
  seoTitle: string | null; seoDescription: string | null; ogImage: string | null;
  stats: Record<string, unknown>;          // { listingCount, medianPrice } when computed
};

type PublicGuideDetailOut = {
  guide: PublicGuideOut;
  listings: PublicListingOut[];
  listingsNextCursor: string | null;
};
```

**Note the detail shape is a wrapper, not a guide with extra fields** — read `response.guide`, not `response`.

`boundary` rings are `[lon, lat]` — **GeoJSON order, the reverse of Leaflet's `[lat, lng]`.** Swap when rendering or your polygon lands in the wrong hemisphere.

Listings inside the boundary are computed **live** (no stored relationship), so the set changes as inventory moves. `stats` is recomputed nightly and may be `{}` on a new guide.

---

## 10.15 Market reports — `/reports/[slug]`

`GET /reports/{slug}?locale=` → `{ slug, title, stats, publishedAt, pdfReady }`

**No download URL is returned.** The PDF is gated behind a lead form.

### Download gate

`POST /reports/{slug}/download` — body `_CaptureBase`, **email required**.

```ts
// → 200 { downloadUrl: string }   — presigned, expires in 15 minutes
```

Trigger the download immediately; do not persist the URL.

| Condition | Response | Copy |
|---|---|---|
| `pdfReady: false` | 409 | *"This report is being prepared. Please check back shortly."* |
| Draft / unknown slug | 404 | Not found |

Show `stats` on the page so the report has visible value before the email wall.

---

## 10.16 Legal pages

| Page | Endpoint |
|---|---|
| `/legal` | `GET /legal` → `{ kind, version, effectiveAt }[]` |
| `/legal/[kind]` | `GET /legal/{kind}?locale=` → `{ kind, version, body, effectiveAt }` |

`kind` ∈ `privacy` `terms` `fair_treatment` `license_disclosure`.

Always serves the **current** version. Display `version` and `effectiveAt` — the versioning exists so an agency can prove what a user agreed to.

---

## 10.17 Review submission

`POST /reviews` — 5/min, honeypot.

```ts
{ agentSlug?: string;    // ≤120 — omit for an agency-wide testimonial
  listingRef?: string;   // ≤120
  rating: number;        // required, 1–5
  title?: string;        // ≤200
  body: string;          // required, 1–4000
  authorName: string;    // required, 1–120
  authorEmail?: string;  // ≤320
  hp: string; renderedAt: string }
// → 201 { id, status: "pending" }
```

**Always lands `pending`.** Set expectations: *"Thank you — your review will appear once approved."* Never imply it is live.

Unknown or unpublished `agentSlug` → 404. Unknown `listingRef` → 404.

---

## 10.18 Saved-search signup (anonymous)

`POST /saved-searches` — 5/min, honeypot. Lets a visitor subscribe to alerts without an account.

```ts
{ email: string;                 // required, ≤320
  name?: string;                 // default "My search", 1–120
  filters?: PublicListingFilters;// same shape as the search page
  frequency?: "instant" | "daily" | "weekly";   // default "daily"
  locale?: string;
  hp: string; renderedAt: string }
// → 201 { id }
```

**Double opt-in.** The row is inactive until confirmed by email:

- `POST /saved-searches/confirm { token }` → `SavedSearchOut` — the confirm landing page
- `POST /saved-searches/unsubscribe { token }` → 204 — idempotent, works forever (stateless HMAC)

Pass the **current search filters** so the CTA on the results page reads "Alert me about these results."

The confirmation also records the marketing consent, so the copy must make the opt-in explicit.

---

## 10.19 Cookie consent banner

`GET /site/cookie-config` → config **or literal `null`** if the agency has not configured one. **`null` means render no banner at all** — handle it, do not crash.

```ts
type CookieConsentConfig = {
  categories: Record<string, unknown>[];   // frontend-defined shape
  bannerCopy: Record<string, unknown>;     // i18n copy
  isEnabled: boolean;
} | null;
```

`POST /consent` — 30/min:

```ts
{ sessionId?: string;   // ≤64
  choices: { necessary?: boolean; analytics?: boolean; marketing?: boolean } }
// → 201 ConsentRecordOut[]
```

**A fully anonymous submission with no `sessionId` is a 409.** Generate a stable session id (UUID in a first-party cookie) before showing the banner, and send it on every consent write.

`necessary` is always-on — render it disabled-and-checked, but still include it in `choices` for a complete record.

**Consent gates analytics.** Until a session has a granted `analytics` record, its events are silently dropped (§10.20). Do not fire analytics before consent.

---

## 10.20 Analytics tracking

`POST /analytics/events` — 120/min, anonymous, batched.

```ts
{ events: AnalyticsEvent[] }   // 1–50 per request
// → 202 { accepted: number }
```

Discriminated union on `eventType`. Every variant may carry `sessionId?`, `listingId?`, `source?`:

| `eventType` | Extra fields |
|---|---|
| `listing_view` | `listingId` **required** |
| `favorite` | `listingId` **required** |
| `search` | `query?` (≤200), `resultsCount?` (≥0) |
| `form_start` / `form_submit` | `form?` (≤60) |
| `page_view` | `path?` (≤500) |

**The allowlist is strict** — an unknown `eventType` or an extra field is a 422 for the whole batch. Do not invent event types.

Buffer client-side and flush on a timer (~5s), on batch size 50, and on `visibilitychange` → hidden.

**`accepted` may be lower than the number sent** and gives no reason — consent-gated drops are deliberately invisible. Do not treat a low count as an error or retry.

> **GAP (Medium) —** Ingestion is anonymous-only; there is no way to attribute events to a signed-in user. Logged-in behaviour cannot feed personalisation or per-user reporting. *Suggested: an optional-auth dependency on the ingest route to record `user_id` when a token is present.*

---

## 10.21 Error pages

| Case | Page |
|---|---|
| **404 unknown host** | Neutral "Site not found". Do not mention the platform or other agencies |
| **402 tenant-suspended** | Full-page maintenance notice. Global boundary — can occur on any request |
| **404 content** | In-layout "not found" with search links |
| **500** | Generic error + retry. Show `requestId` from the problem body — it makes support tractable |

---

# PART III — BUYER ACCOUNT

**Route group:** `/account/*` · **Rendering:** client · **Auth:** bearer token required · **`noindex`**

Every endpoint in this part is **👤 ownership-authorized**. There is no RBAC permission — you may only ever act on your own rows. Any authenticated tenant user reaches these pages, including agency staff (an agent can have favourites too).

**Guard:** redirect to `/login?next=<path>` when unauthenticated. Wait for the silent-refresh bootstrap (§4.3) before deciding — redirecting during bootstrap logs out every returning user on a hard refresh.

---

## 11.1 Account dashboard — `/account`

Summary cards linking to each section:

| Card | Source |
|---|---|
| Saved properties | `GET /me/favorites?limit=1` → `totalEstimate` is null; use `items.length` or fetch the list |
| Saved searches | `GET /me/saved-searches` → plain array, use `.length` |
| Upcoming tours | `GET /me/appointments?upcomingOnly=true&limit=3` → `totalEstimate` **is** populated |
| Unread notifications | `GET /me/notifications/unread-count` → `{ unread }` |

Also render the email-verification banner while `user.emailVerifiedAt` is `null`, with a resend action (`POST /auth/verify-email/request`).

---

## 11.2 Favorites — `/account/favorites`

| Action | Endpoint | Result |
|---|---|---|
| Add | `PUT /me/favorites/{listingId}` | 204, **idempotent** |
| Remove | `DELETE /me/favorites/{listingId}` | 204, idempotent |
| List | `GET /me/favorites?cursor=&limit=&locale=` | `Page<FavoriteItemOut>` |

```ts
type FavoriteItemOut = { favoritedAt: string; listing: PublicListingOut };
```

**Both writes are idempotent** — favouriting twice is not an error. This makes the toggle safe to apply optimistically (§9.7).

**Only published listings can be favourited.** An unpublished/unknown id is a 404.

**Unpublished listings drop out of the list but the row survives.** If a property is temporarily unpublished it disappears from `/account/favorites` and returns if relisted. Do not delete client-side state on absence — and consider copy acknowledging the list shows currently-available properties.

`totalEstimate` is null here.

**Empty state:** *"You haven't saved any properties yet."* + link to search.

**The favourite button lives on every listing card and detail page**, not only in the account area. It requires auth — for an anonymous user, prompt sign-in and complete the action after login (preserve the intent through the redirect).

---

## 11.3 Saved searches — `/account/searches`

| Action | Endpoint |
|---|---|
| List | `GET /me/saved-searches` → **plain array**, not paginated |
| Get | `GET /me/saved-searches/{id}` |
| Create | `POST /me/saved-searches` → 201 |
| Update | `PATCH /me/saved-searches/{id}` |
| Delete | `DELETE /me/saved-searches/{id}` → 204 |

```ts
type SavedSearchOut = {
  id: string; name: string;
  filters: Record<string, unknown>;   // the PublicListingFilters shape
  frequency: "instant" | "daily" | "weekly";
  locale: string; isActive: boolean;
  lastRunAt: string | null;
  createdAt: string; updatedAt: string;
};

// create
{ name: string;              // 1–120
  filters?: PublicListingFilters;
  frequency?: AlertFrequency;   // default "instant"
  locale?: string }
```

**Capped at 20 per user** — exceeding is a 409. Show the count (`n/20`) and disable "Save this search" at the limit with an explanatory message.

**PATCH rejects explicit `null`** on `name`, `filters`, `frequency`, `isActive`, `locale` (422). Omit a field to leave it unchanged; never send `null` to "clear" it.

**`locale` is pinned at creation** so full-text `q` terms replay under the same language configuration. Changing it changes matching behaviour — surface it as "Alert language."

**Alert semantics:**
- `instant` — emailed when a matching listing is published
- `daily` — daily digest
- `weekly` — Mondays

Matching runs through the *same* query builder as the search page, so alerts can never disagree with on-site results.

**`isActive: false`** = paused. Offer a pause toggle rather than forcing deletion.

**Entry point:** a "Save this search" CTA on `/listings` that passes the current filters. For an anonymous visitor, route to the double-opt-in flow (§10.18) instead.

---

## 11.4 My tours — `/account/tours`

`GET /me/appointments?upcomingOnly=&cursor=&limit=` → `Page<MyAppointmentOut>` — `totalEstimate` **is** populated.

```ts
type MyAppointmentOut = {
  id: string;
  agentUserId: string;
  listingId: string | null;
  status: "requested" | "confirmed" | "completed" | "cancelled" | "no_show";
  startAt: string; endAt: string;
  confirmedAt: string | null;
  createdAt: string;
};
```

**Ordered by `startAt` ascending.** `upcomingOnly=true` returns only future tours still in `requested` or `confirmed`; the default (`false`) returns the full history including past and cancelled.

Suggested layout: two tabs — **Upcoming** (`upcomingOnly=true`) and **History** (default).

### Two behaviours that will generate support tickets if not handled

**1. An unverified email returns an empty list — always.** Tours are booked anonymously and joined to the account by *verified* email. An account that never confirmed its address sees nothing, with a 200 and no error.

> If `user.emailVerifiedAt === null`, do not render a bare empty state. Render: *"Verify your email address to see tours you've booked."* with a resend button. This is the single most important piece of copy in the buyer account.

**2. A tour booked with a different email will not appear.** The join is by email address, not identity. Someone who books as `me@work.com` and registers as `me@home.com` sees nothing. Consider a hint in the empty state: *"Only tours booked with your account email appear here."*

### Rendering

- Status badge (§7.2 conventions)
- **Times in the agency's timezone**, not the browser's (§7.4)
- Link to the listing when `listingId` is set
- `requested` → *"Awaiting confirmation from the agency"*
- `confirmed` → show `confirmedAt`, offer an "Add to calendar" `.ics` generated client-side

> **GAP (Medium) —** There is **no buyer-side cancel**. Only agency staff can transition an appointment (`POST /portal/appointments/{id}/status`). A visitor who cannot attend has no way to say so, which produces no-shows the agency then penalises in the lead score. *Suggested: `POST /me/appointments/{id}/cancel`, ownership-scoped by the same verified-email join, permitting `requested|confirmed → cancelled` only.*

> **GAP (Low) —** `agentUserId` is a raw UUID with no name, slug or photo, and there is no endpoint mapping a user id to a public agent profile. The buyer cannot see *who* they are meeting. *Suggested: include a minimal `agent: {slug, displayName}` on `MyAppointmentOut`.*

---

## 11.5 Notifications — `/account/notifications`

| Action | Endpoint |
|---|---|
| List | `GET /me/notifications?unreadOnly=&cursor=&limit=` → `Page<NotificationOut>` |
| Unread count | `GET /me/notifications/unread-count` → `{ unread: number }` |
| Mark read | `POST /me/notifications/mark-read` → 204 |
| WS ticket | `POST /me/notifications/ws-ticket` → `{ ticket, expiresIn }` |

```ts
type NotificationOut = {
  id: string;
  type: NotificationType;
  payload: Record<string, unknown>;   // shape varies by type — read defensively
  readAt: string | null;
  createdAt: string;
};

// mark-read — both fields optional; sending neither is a no-op
{ ids?: string[]; all?: boolean }
```

**Six notification types** (all currently staff-oriented — see the gap below):

`lead_assigned` · `lead_escalated` · `appointment_reminder` · `appointment_confirmed` · `appointment_cancelled` · `milestone_due`

Render a friendly line per type from `payload`; **fall back to a generic message for an unknown type** rather than crashing — new types can ship without a frontend release.

**Live delivery** via WebSocket (§9.6). On every connect or reconnect, refetch the list and unread count — pub/sub is best-effort and anything sent while disconnected is lost.

**Badge:** drive from `unread-count`; update optimistically on mark-read, and refetch on WS message.

> **GAP (Low) —** All six notification types are agency-facing. A buyer currently receives **nothing** through this system — tour confirmations and alert digests go out as direct emails and create no in-app row. The buyer's notification centre will be permanently empty. Either hide it from the buyer navigation for now, or request buyer-facing notification types (`tour_confirmed`, `saved_search_match`).

---

## 11.6 Notification preferences — `/account/settings/notifications`

`GET /me/notifications/preferences` → `PreferencesOut` · `PUT` same shape → `PreferencesOut`

```ts
type PreferencesOut = {
  types: {
    type: NotificationType;
    digestEligible: boolean;
    channels: { channel: "in_app" | "email" | "sms" | "whatsapp"; enabled: boolean }[];
  }[];
};

// PUT — partial: only the (type, channel) pairs you send are changed
{ types: [{ type: NotificationType; channels: [{ channel; enabled }] }] }
```

Render as a **type × channel matrix**. The GET always returns every type with every channel, so the grid is complete without any defaults on the client.

**Two things the UI must get right:**

1. **SMS and WhatsApp have no delivery adapter.** Enabling them silently does nothing — the send is logged `skipped`. Render them **disabled with a "Coming soon" label**, not as working toggles. A toggle that appears to work and never delivers is worse than no toggle.

2. **`digestEligible` is `false` for all six current types**, so quiet-hours batching never triggers. Do not build a quiet-hours UI yet — there is nothing for it to affect.

**PUT is partial.** Send only what changed; omitted pairs are untouched.

---

## 11.7 Profile — `/account/settings`

`GET /users/me` → `UserOut` · `PATCH /users/me` → `UserOut`

```ts
type UserOut = {
  id: string; email: string; role: Role; status: string;
  firstName: string | null; lastName: string | null;
  locale: string; phone: string | null;
  emailVerifiedAt: string | null; lastLoginAt: string | null;
  createdAt: string;
};

// PATCH — deliberately narrow
{ firstName?: string;  // ≤80
  lastName?: string;   // ≤80
  locale?: string;     // 2–10
  phone?: string }     // ≤32
```

**Email and role are not self-editable.** Render email read-only with the verification state. There is no self-service email-change flow.

> **GAP (Low) —** No password-change endpoint for a signed-in user. The only path is the forgot-password flow (`POST /auth/password/forgot` → email → reset). Label the button "Reset password by email" so the behaviour is not a surprise. *Suggested: `POST /auth/password/change {currentPassword, newPassword}`.*

Changing `locale` should also switch the UI locale immediately.

---

## 11.8 Security — `/account/settings/security`

Combines sessions and MFA (§4.6).

**Sessions:** `GET /auth/sessions` → list; `DELETE /auth/sessions/{id}` → revoke.

Show `userAgent` (parsed to a friendly device name), `ip`, `lastUsedAt`, and mark the row with `current: true` as "This device". Disable revoking the current session — use "Sign out" instead. Offer "Sign out everywhere" → `POST /auth/logout-all`.

Only live sessions are listed; revoked and expired rows are excluded.

**MFA:**
1. `GET /auth/mfa/status` → `{ enabled, enrolledAt }`
2. Enrol → `POST /auth/mfa/enrol` → render `provisioningUri` as a QR code, `secret` as copyable text
3. Confirm → `POST /auth/mfa/enrol/confirm { code }` → 204. **Only now is MFA active**
4. Disable → `POST /auth/mfa/disable { password }` → 204

**Re-enrolling while already enabled is allowed** (a lost phone) and does **not** disable the live factor until the new one is confirmed. Make that explicit in the UI: the old authenticator keeps working until you confirm the new one.

An abandoned enrolment leaves the account unchanged.

---

## 11.9 Privacy — `/account/settings/privacy`

### Export my data

`GET /me/export` → `DataExportOut` — served **inline**, no job to poll.

```ts
type DataExportOut = {
  subjectUserId: string;
  subjectEmail: string | null;
  exportedAt: string;
  sections: Record<string, unknown>;   // account, CRM footprint, favorites, searches,
                                       // notifications, consent history
};
```

Trigger a client-side JSON download. May be large — show a progress state while fetching.

### Delete my account

`DELETE /me` → **202** `{ requestId, purgeScheduledAt }`

This is destructive and requires a **typed-confirmation** dialog, not a plain "Are you sure?".

**Explain what actually happens**, because it is nuanced and the copy is a compliance surface:

- The account is soft-deleted **immediately** and you are signed out on every device at once
- Permanent purge happens on `purgeScheduledAt` (**30 days**)
- Favourites, saved searches and notifications are **deleted permanently**
- CRM records (your enquiries) are **anonymised, not deleted** — the agency keeps the business record with your personal details stripped

**All tokens are revoked immediately** — the very next API call 401s. Redirect to a confirmation page and clear all client state.

**Idempotent:** repeating while one is pending returns the existing request rather than creating a second.

`GET /me/dsr/{dsrId}` → status of a request (404 if not yours):

```ts
{ id, kind: "export" | "erasure",
  status: "pending" | "completed" | "cancelled",
  purgeScheduledAt: string | null, completedAt: string | null,
  result: Record<string, unknown>, createdAt: string }
```

> **GAP (Low) —** There is no *cancel* endpoint for a pending erasure. Once submitted, the 30-day countdown cannot be stopped through the API. Say so explicitly in the confirmation dialog. *Suggested: `POST /me/dsr/{id}/cancel` while `status === "pending"`.*

> **GAP (Low) —** `GET /me/export` creates no DSR row, so an export never appears in `/me/dsr/{id}`. Only erasures are trackable.

### Consent

> **GAP (Medium) —** A signed-in user cannot view or change their consent choices. `POST /consent` is anonymous and session-keyed; there is no `GET /me/consent` and no authenticated write. The privacy page can therefore only offer the cookie banner again — it cannot show "you agreed to marketing email on 3 March." *Suggested: `GET /me/consent` (history) and `POST /me/consent` (authenticated record).*

---

# PART IV — AGENCY PORTAL

**Route group:** `/portal/*` · **Rendering:** client · **Auth:** bearer · **`noindex`**

## 12.0 The two-axis authorization model

This is the most important concept in the portal. **Permission and visibility are separate axes**, and confusing them produces either a broken UI or a security hole.

| Axis | Question | Mechanism | Failure |
|---|---|---|---|
| **Permission** | *May this role do X at all?* | `require(Permission.X)` | **403** |
| **Visibility scope** | *Which rows may this actor see?* | `scope_user_ids_for` | **404** (row filtered out) |

### Permission matrix — complete and authoritative

| Permission | Agent | Team Lead | Marketing | Admin |
|---|:---:|:---:|:---:|:---:|
| `listing:manage` | ✅ | ✅ | ✅ | ✅ |
| `listing:publish` | — | ✅ | — | ✅ |
| `lead:manage` | ✅ | ✅ | ✅ | ✅ |
| `lead:view_all` | — | ✅ | ✅ | ✅ |
| `lead:assign` | — | ✅ | ✅ | ✅ |
| `agent:manage` | — | ✅ | ✅ | ✅ |
| `appointment:manage` | ✅ | ✅ | ✅ | ✅ |
| `deal:manage` | ✅ | ✅ | **—** | ✅ |
| `content:manage` | — | — | ✅ | ✅ |
| `review:moderate` | — | — | ✅ | ✅ |
| `analytics:view` | — | ✅ | ✅ | ✅ |
| `compliance:manage` | — | — | — | ✅ |
| `webhook:manage` | — | — | — | ✅ |
| `user:view` / `user:manage` | — | — | — | ✅ |

`buyer_renter` and `seller` hold **no** permissions — they never see the portal.

**Note the two surprises:** marketing has **no** `deal:manage` (commissions are sensitive and a marketer has no reason to see a back-office deal), and team lead has **no** `content:manage` or `review:moderate` (site content is marketing's job).

### Visibility scope

| Role | Sees |
|---|---|
| **Admin**, **Marketing** | Tenant-wide — everything |
| **Team Lead** | Self + members of teams they lead |
| **Agent** | Own rows only |

Applies to: portal listings, leads, appointments, deals, and the per-listing analytics report.

**An out-of-scope row returns 404, not 403.** Never write UI that says "you don't have access to this listing" on a 404 — it may not exist at all.

### Client-side gating helper

```ts
const ROLE_PERMISSIONS: Record<Role, Permission[]> = {
  buyer_renter: [], seller: [],
  agent: ["listing:manage", "lead:manage", "appointment:manage", "deal:manage"],
  team_lead: ["listing:manage", "listing:publish", "lead:manage", "lead:view_all",
              "lead:assign", "agent:manage", "appointment:manage", "deal:manage",
              "analytics:view"],
  marketing: ["listing:manage", "lead:manage", "lead:view_all", "lead:assign",
              "agent:manage", "appointment:manage", "content:manage",
              "review:moderate", "analytics:view"],
  admin: ["user:view", "user:manage", "listing:manage", "listing:publish",
          "lead:manage", "lead:view_all", "lead:assign", "agent:manage",
          "appointment:manage", "content:manage", "review:moderate",
          "deal:manage", "analytics:view", "compliance:manage", "webhook:manage"],
  platform_admin: ["platform:tenant:view", "platform:tenant:manage", "platform:staff:manage"],
  platform_support: ["platform:tenant:view"],
};

export const can = (role: Role, p: Permission) => ROLE_PERMISSIONS[role].includes(p);
```

**This table is a mirror, not the source of truth.** The server enforces independently. Keeping it client-side avoids rendering doomed navigation, but never rely on it for security — and keep it in sync when the backend matrix changes.

---

## 12.1 Portal shell

**Navigation, gated by permission:**

| Item | Route | Gate |
|---|---|---|
| Dashboard | `/portal` | — |
| Listings | `/portal/listings` | `listing:manage` |
| Leads | `/portal/leads` | `lead:manage` |
| Contacts | `/portal/contacts` | `lead:view_all` |
| Tours | `/portal/tours` | `appointment:manage` |
| Deals | `/portal/deals` | `deal:manage` |
| Agents | `/portal/agents` | `agent:manage` (or own profile for an agent) |
| Teams | `/portal/teams` | `agent:manage` |
| Content | `/portal/content` | `content:manage` |
| Blog | `/portal/blog` | `content:manage` |
| Reviews | `/portal/reviews` | `review:moderate` |
| Analytics | `/portal/analytics` | `analytics:view` (per-listing report: authenticated only) |
| Syndication | `/portal/syndication` | `listing:manage` |
| Webhooks | `/portal/webhooks` | `webhook:manage` |
| Users | `/portal/users` | `user:view` |
| Compliance | `/portal/compliance` | `compliance:manage` |

**Hide what the role cannot use.** Do not render a disabled item the user can never enable.

**Impersonation banner:** if the session was minted by platform staff, the token carries an impersonation marker and the login response includes `impersonation: true`. Render a persistent, high-contrast banner: *"You are viewing as {tenant}. Session ends in {n} minutes."* The token is time-boxed (15 min) and **cannot be refreshed** — on 401, return to the platform back-office rather than showing a login form.

---

## 12.2 Listings

### 12.2.1 List — `/portal/listings` 🔒 `listing:manage`

`GET /portal/listings` → `Page<ListingOut>` — **`totalEstimate` is populated**.

| Param | Notes |
|---|---|
| `status` | One `ListingStatus` |
| `q` | ≤200 — matches **reference code, title (any locale), city** |
| `sort` | `newest` (default) · `updated` · `price_asc` · `price_desc` |
| `cursor` / `limit` | 1–100, default 24 |

**`q` is a substring match, not full-text.** It intentionally covers the reference code, which the public full-text search does not index — agents search by code far more than by prose. Partial words match.

**`sort` differs from the public search** — there is no `featured`-first rule (that is paid public placement) and there is an `updated` option ("what did I touch last").

**Changing `sort` invalidates the cursor** — a mismatched cursor is a 400. Reset pagination on sort change.

**Scoped:** an agent sees only their own listings, including through search.

Table columns: cover thumb, reference code, title (default locale), status badge, price, city, agent, updated. Bulk selection is not supported by the API — no bulk endpoints exist.

### 12.2.2 Create / edit — `/portal/listings/new`, `/portal/listings/[id]`

`POST /portal/listings` → 201 · `PATCH /portal/listings/{id}` · `GET /portal/listings/{id}`

```ts
type ListingCreate = {
  purpose: "sale" | "rent" | "rent_daily";        // required, IMMUTABLE after create
  propertyType: PropertyType;                     // required
  title: Record<string, string>;                  // required, ≥1 locale, ≤200 each
  description?: Record<string, string>;           // ≤10000 each
  price: string;                                  // required, >0, ≤999999999999, 2dp
  currency?: string;                              // ^[A-Z]{3}$, default "DZD"
  negotiable?: boolean;
  beds?: number; baths?: number;                  // 0–100
  areaBuilt?: string; areaLand?: string;          // >0, ≤99999999
  floor?: number;                                 // -5..200
  floorsTotal?: number;                           // 1..200
  yearBuilt?: number;                             // ≥1800, ≤ current year + 5
  features?: string[];                            // from the 18-value vocabulary
  address?: { line1?; line2?; city?; state?; postalCode?; country? };  // country ≤2
  location?: { lat: number; lng: number };
  agentId?: string;
  expiresAt?: string;
};
```

**Validation the form must mirror:**
- `floor ≤ floorsTotal` when both are set
- Title requires at least one non-whitespace locale
- Features must come from the vocabulary — use a multi-select, never free text
- `purpose` **cannot be changed** after creation (422). Disable it in edit mode
- `pricePeriod` is derived server-side (`rent`→`month`, `rent_daily`→`day`, `sale`→null). Read-only

**PATCH rejects explicit `null`** for `title`, `price`, `currency`, `negotiable`, `features`, `featured` (422). Omit to leave unchanged. `location` and `description` **are** nullable — send `null` to clear.

**Manager-only fields** (`admin`, `team_lead`, `marketing`):
- `agentId` — reassigning to another user. An agent sending another user's id gets a **403**. `agentId: null` unassigns
- `featured` — paid public placement

### 12.2.3 Workflow — `POST /portal/listings/{id}/transition`

Body `{ toStatus }`. Legal transitions:

```
draft     → review, published, archived
review    → draft, published, archived
published → reserved, sold, rented, archived
reserved  → published, sold, rented, archived
sold      → archived
rented    → archived
archived  → draft                    ← the relist path
```

**Only render buttons for legal targets from the current status.** An illegal transition is a 409.

**Publishing requires `listing:publish`** — held by admin and team lead only — *unless* the agency enables `settings.listings.agent_self_publish`. An agent without either gets a 403. Read the setting from `GET /site/config` to decide whether to show the button, but expect the 403 anyway.

**Publishing triggers, asynchronously:** saved-search alert matching, portal syndication, and a `listing.published` webhook. None block the response; do not wait for them.

### 12.2.4 Other actions

| Action | Endpoint | Notes |
|---|---|---|
| Duplicate | `POST /portal/listings/{id}/duplicate` | 201, new draft, fresh reference code. **Media is not copied** |
| History | `GET /portal/listings/{id}/history` | `{id, fromStatus, toStatus, changedBy?, createdAt}[]` |
| Delete | `DELETE /portal/listings/{id}` | 204. **409 while published/reserved/sold/rented** — archive first |
| AI description | `POST /portal/listings/{id}/generate-description` | See below |

**AI description generation:**

```ts
{ locales?: string[];   // defaults to all supported; validated against ar/fr/en
  tone?: string }
// → { description: Record<string, string>; model: string }
```

**The result is a draft and is never saved.** The agent must review and then PATCH it onto the listing. Present it in an editable panel with explicit "Use this" / "Discard" actions — never write it silently.

Synchronous and slow (up to ~30s): show a proper loading state. **503 `upstream-unavailable`** means the AI provider failed — copy: *"Description generation is unavailable right now. Please write the description manually or try again later."*

### 12.2.5 Media — `/portal/listings/[id]/media` 🔒 `listing:manage`

| Action | Endpoint |
|---|---|
| List | `GET /portal/listings/{id}/media` |
| Presign upload | `POST /portal/listings/{id}/media/uploads` → 201 |
| Confirm | `POST /portal/media/{mediaId}/confirm` → 202 |
| Add embed | `POST /portal/listings/{id}/media/embeds` → 201 |
| Update | `PATCH /portal/media/{mediaId}` |
| Delete | `DELETE /portal/media/{mediaId}` → 204 |
| Download (private) | `GET /portal/media/{mediaId}/download` → `{downloadUrl, expiresInSeconds}` |

**Upload:** the three-step flow in §9.4. Presign body `{ kind, contentType, sizeBytes, altText? }`.

| Kind | Allowed content types |
|---|---|
| `photo` | jpeg, png, webp |
| `floorplan` | jpeg, png, webp, pdf |
| `doc` | pdf only |

**Embeds** (`video`, `tour_3d`) take a URL instead of a file: `{ kind, url, altText? }`. Hosts are allowlisted server-side — YouTube, Vimeo, Matterport. Anything else is a 422.

**Status polling is mandatory** (§9.3): `pending → processing → ready | failed`. A `failed` row carries an `error` string — show it; failure is permanent (bad magic bytes, wrong type) and is never retried.

**Update** `{ position?, altText?, isCover? }`. Setting `isCover: true` clears the previous cover automatically (a partial-unique index enforces one cover per listing).

**Drag-and-drop reordering fires one PATCH per moved item** — there is no bulk reorder endpoint. Debounce and send sequentially.

**Quota:** upload reserves the *declared* size against the plan's storage. 403 `quota-exceeded` when full. A per-listing photo cap may also apply via `settings.media.max_photos_per_listing`.

---

## 12.3 Leads

### 12.3.1 Inbox — `/portal/leads` 🔒 `lead:manage`

`GET /portal/leads?stage=&agentId=&source=&listingId=&cursor=&limit=` → `Page<LeadOut>`

```ts
type LeadOut = {
  id: string; contactId: string;
  listingId: string | null; agentId: string | null;
  source: LeadSource; sourceMeta: Record<string, unknown>;
  stage: LeadStage; score: number;          // 0–100
  lostReason: string | null;
  firstResponseAt: string | null;
  createdAt: string; updatedAt: string;
};
```

**Scoped** — an agent sees only leads assigned to them.

> **GAP (Medium) —** `LeadOut` carries only `contactId`, not the contact's name, email or phone. An inbox showing "Lead #a3f2… score 72" is unusable. There is **no batch contact endpoint** — `GET /portal/contacts/{id}` is one-at-a-time and requires `lead:view_all`, which **agents do not have**. An agent literally cannot see who their own lead is from the list. *Suggested: embed a minimal `contact: {firstName, lastName, email, phone}` in `LeadOut`, or add `GET /portal/contacts?ids=`.*
>
> **Interim workaround:** open `GET /portal/leads/{id}` per row — it returns `LeadDetailOut` with the full contact embedded — but that is N+1 and only viable for a page of 24.

Suggested layout: a stage-column board (kanban) or a table with a stage filter. Sort is fixed (newest first); there is no sort parameter.

**Score** is 0–100 from source weight, listing attachment, engagement, recency decay and no-show penalty. Render as a badge with a tier (hot ≥70 / warm 40–69 / cold <40) — the number alone means little to an agent.

**`firstResponseAt === null` on an old lead is the key operational signal.** Highlight unanswered leads prominently; speed-to-lead is the product's core value.

### 12.3.2 Detail — `/portal/leads/[id]`

`GET /portal/leads/{id}` → `LeadDetailOut` = `LeadOut` + `contact: ContactOut`

```ts
type ContactOut = {
  id: string; firstName: string | null; lastName: string | null;
  email: string | null; phone: string | null; whatsapp: string | null;
  consent: Record<string, unknown>; tags: string[];
  notes: string | null; createdAt: string; updatedAt: string;
};
```

**Actions:**

| Action | Endpoint | Notes |
|---|---|---|
| Update | `PATCH /portal/leads/{id}` | `{agentId?, listingId?}` **only** |
| Change stage | `POST /portal/leads/{id}/stage` | `{toStage, lostReason?}` |
| Log activity | `POST /portal/leads/{id}/activities` | `{type, payload}` |
| Timeline | `GET /portal/leads/{id}/activities` | `Page<ActivityOut>` |

**Stage transitions are unrestricted** — any stage to any stage. The one rule: **moving to `lost` requires `lostReason`** (409 without it). Prompt for it.

Stages: `new` `contacted` `qualified` `touring` `offer` `won` `lost`

**Reassignment (`agentId`) is manager-only** — agent, and marketing/team-lead/admin. An agent attempting it gets a 403. Hide the control.

**Activity types the client may log:** `note` `call` `email` `sms` `tour`
**System-only (422 if posted):** `status_change` `assignment` `no_show` `system`

**Logging an agent-authored activity sets `firstResponseAt`** and stops the drip sequence — it is the reply proxy. Make that visible: *"Logging a call or note marks this lead as responded and stops automated follow-up."*

### 12.3.3 Contacts — `/portal/contacts` 🔒 `lead:view_all`

Note the different permission — **agents cannot access contacts directly.** Contacts are not agent-owned, so an agent must not edit one via a leaked UUID.

| Action | Endpoint |
|---|---|
| Get | `GET /portal/contacts/{id}` |
| Update | `PATCH /portal/contacts/{id}` |
| Timeline | `GET /portal/contacts/{id}/timeline` |

```ts
type ContactTimelineOut = {
  contact: ContactOut;
  leads: LeadOut[];
  entries: { kind: "lead_created" | "activity"; at: string;
             leadId: string; activity?: ActivityOut; leadStage?: LeadStage }[];
};
```

> **GAP (Low) —** There is **no contact list endpoint** — only get-by-id. A "browse all contacts" page cannot be built; contacts are reachable only from a lead. Route `/portal/contacts` to a search-by-lead view, or omit the nav item. *Suggested: `GET /portal/contacts?q=&cursor=`.*

### 12.3.4 Assignment rules — `/portal/leads/settings` 🔒 `lead:assign`

`GET` / `PUT /portal/leads/assignment-rule`

```ts
{ strategy: "listing_agent" | "round_robin" | "territory";
  config: { agentPool?: string[]; maxOpenLeadsPerAgent?: number } }   // ≥1
```

| Strategy | Behaviour |
|---|---|
| `listing_agent` | Route to the listing's assigned agent (default) |
| `round_robin` | Least-loaded agent from `agentPool`, or all active agents |
| `territory` | Match the listing's location against agents' service areas |

**`territory` requires agents to have service-area polygons.** Warn if none are configured — leads will fall through to unassigned.

Unassigned leads escalate to admins after 30 minutes.

---

## 12.4 Agents & teams

### 12.4.1 Agent profiles — `/portal/agents`

| Action | Endpoint | Gate |
|---|---|---|
| Roster | `GET /portal/agents` | 🔒 `agent:manage` — **manager-only, unscoped** |
| Own profile | `GET /portal/agents/me` | 👤 authenticated (404 if none) |
| Get | `GET /portal/agents/{id}` | 👤 own, or manager |
| Create | `POST /portal/agents` | 👤 self always; for another user, manager |
| Update | `PATCH /portal/agents/{id}` | 👤 own, or manager |
| Delete | `DELETE /portal/agents/{id}` | 👤 own, or manager |
| Stats | `GET /portal/agents/{id}/stats` | 👤 own, or manager |

**Note the pattern:** individual profile operations are *ownership*-gated (any authenticated user, service-checked), while the full roster requires `agent:manage`. An agent can manage their own profile without holding that permission.

```ts
type AgentProfileCreate = {
  userId?: string;              // defaults to caller
  slug: string;                 // 2–120, ^[a-z0-9]+(-[a-z0-9]+)*$
  bio?: Record<string, string>; // ≤5000 per locale
  specialties?: string[];       // from the 10-value vocabulary
  serviceAreas?: [number, number][][];   // rings of [lon, lat]; ≤10 areas, ≤100 pts each
  licenseNo?: string;           // ≤100
  whatsappNumber?: string;      // E.164: ^\+[1-9]\d{6,14}$
  socials?: Record<string, string>;      // https URLs, ≤300
};
```

- **Slug collision → 409.** Suggest an alternative
- **`isPublished` is manager-only** (PATCH). An agent cannot self-publish into the public directory
- **One profile per user** — a second create is a 409
- **Agent-seat quota** applies — 403 `quota-exceeded`
- `serviceAreas` rings are `[lon, lat]` — reversed from Leaflet
- `whatsappNumber` is **portal-only**; it never appears on the public profile. The server mints the wa.me link

**Photo:** presign → PUT → confirm (§9.4), then poll `photoStatus` until `ready`/`failed`. Replacing a photo cleans up the old objects automatically.

**Stats:**
```ts
{ userId, listingsByStatus: Record<string, number>,
  leadsByStage: Record<string, number>,
  avgFirstResponseSeconds: number | null,
  reviews: { count: number; average: number | null } }
```

### 12.4.2 Teams — `/portal/teams` 🔒 `agent:manage` (router-wide)

| Action | Endpoint |
|---|---|
| List / create | `GET` / `POST /portal/teams` |
| Get / update / delete | `GET` / `PATCH` / `DELETE /portal/teams/{id}` |
| Members | `GET` / `POST /portal/teams/{id}/members` |
| Remove member | `DELETE /portal/teams/{id}/members/{userId}` |

`TeamCreate { name: string; leadUserId?: string }` · `TeamMemberAdd { userId: string; roleInTeam?: string }`

**An admin manages any team; a non-admin manages only the team they lead** (404 otherwise — no oracle).

**Members must be active `agent` or `team_lead` accounts.**

**Team membership drives visibility scope** — adding an agent to a team immediately widens what that team's lead can see across listings, leads, appointments and deals. Say so in the UI; it is not obvious.

---

## 12.5 Tours — `/portal/tours` 🔒 `appointment:manage`

### Agenda

`GET /portal/appointments?status=&startFrom=&startTo=&cursor=&limit=` → `Page<AppointmentOut>`

```ts
type AppointmentOut = {
  id: string; agentUserId: string;
  listingId: string | null; contactId: string; leadId: string | null;
  status: AppointmentStatus;
  startAt: string; endAt: string; confirmedAt: string | null;
  // NOTE the capital H — the camelCase converter produces "24H"/"1H", not "24h"/"1h".
  reminder24HSentAt: string | null; reminder1HSentAt: string | null;
  createdAt: string; updatedAt: string;
};
```

Ordered `startAt` ascending. **Scoped** — an agent sees their own calendar.

### Transitions — `POST /portal/appointments/{id}/status`

```
requested → confirmed | cancelled
confirmed → completed | cancelled | no_show
completed, cancelled, no_show → terminal
```

**`requested` is never a valid target** — the schema rejects it (422). There is no undo.

- `confirmed` and `cancelled` email the contact automatically
- `no_show` logs a CRM activity and **subtracts 15 from the lead score**. Warn before applying

Render only legal targets for the current status.

> **GAP (Medium) —** Same as the leads inbox: `AppointmentOut` carries `contactId` but no contact name or phone, and contact lookup requires `lead:view_all` — which **agents do not have**. An agent cannot see who they are meeting. *Suggested: embed a minimal contact summary in `AppointmentOut`.*

### Availability — `/portal/agents/[id]/availability`

`GET` / `PUT /portal/agents/{profileId}/availability` — 👤 own or manager.

**PUT is a full replacement.** Send the complete rule set every time; omitted rules are deleted. Max 100 rules.

```ts
type AvailabilityRuleIn = {
  dayOfWeek?: number;   // 0=Monday … 6=Sunday — weekly template
  date?: string;        // YYYY-MM-DD — dated exception
  startTime: string;    // "09:00:00"
  endTime: string;      // must be > startTime
  isBlock?: boolean;    // only valid with `date`
};
```

**Exactly one of `dayOfWeek` or `date`** (422 otherwise). Only dated rules may block.

Model: weekly template + dated additions − dated blocks. Slot length and buffer come from `settings.appointments.*`.

**Times are in the agency timezone**, not UTC — unlike appointments themselves. Do not convert.

### iCal

`GET /portal/agents/{profileId}/ical` → `{ url }` — a secret URL for Google/Apple/Outlook. Offer copy-to-clipboard and warn that anyone with the link can read the calendar.

---

## 12.6 Deals — `/portal/deals` 🔒 `deal:manage`

**Marketing has no access at all** — omit the nav item entirely for that role.

### List & detail

`GET /portal/deals?status=&cursor=&limit=` · `GET /portal/deals/{id}` · **Scoped**

### The commission gate — read carefully

```ts
type DealOut = {
  id: string; ownerUserId: string; title: string; status: DealStatus;
  listingId: string | null; leadId: string | null; contactId: string | null;
  price: string | null; currency: string;
  closedAt: string | null; lostReason: string | null; notes: string | null;
  createdAt: string; updatedAt: string;
};

type DealWithCommissionOut = DealOut & {
  commissionBasis: "percentage" | "flat" | null;
  commissionRate: string | null;
  commissionAmount: string | null;
};
```

**Only `admin` sees commission fields.** For every other role the keys are **absent from the JSON entirely** — not null, not empty. The response type varies per instance.

```ts
const hasCommission = (d: DealOut | DealWithCommissionOut): d is DealWithCommissionOut =>
  "commissionAmount" in d;
```

**Branch on key presence, never on role.** And `GET`/`PUT /portal/deals/{id}/commission` are **403 for non-admins** even though they hold `deal:manage`.

### CRUD

```ts
type DealCreate = {
  title: string;                 // 1–255
  listingId?: string; leadId?: string; contactId?: string;
  ownerUserId?: string;          // defaults to creator
  price?: string; currency?: string;   // default "DZD"
  notes?: string;                // ≤5000
  seedMilestones?: boolean;      // default true → seeds 5 default milestones
};
```

**All optional ids are validated before insert** — a bogus `listingId`, `leadId`, `contactId` or `ownerUserId` is a clean 404, never a 500.

`seedMilestones: true` creates: Offer accepted → Deposit received → Contract signed → Financing approved → Closing.

### Status — `POST /portal/deals/{id}/status`

```
open           → under_contract | closed_won | closed_lost
under_contract → open | closed_won | closed_lost
closed_won, closed_lost → terminal
```

**`closed_lost` requires `lostReason`** (409 otherwise). Closing fires a `deal.closed` webhook — the payload's `outcome` field distinguishes won from lost.

### Commission — admin only

`GET` / `PUT /portal/deals/{id}/commission`

```ts
{ basis: "percentage" | "flat";
  rate?: string;      // required when percentage, 0–100, 3dp
  amount?: string }   // required when flat
```

Percentage derives `commissionAmount` from `price × rate/100`. Show the computed figure live.

### Milestones

`GET`/`POST /portal/deals/{id}/milestones` · `PATCH`/`DELETE /portal/deals/{id}/milestones/{mid}`

```ts
type MilestoneOut = {
  id: string; dealId: string; title: string;
  dueDate: string | null; ownerUserId: string | null;
  completedAt: string | null; position: number;
  createdAt: string; updatedAt: string;
};
```

An hourly sweep notifies the owner (or the deal owner) of milestones due today or earlier. **Changing `dueDate` on an incomplete milestone re-arms the reminder.**

### Documents

Presign → PUT → confirm (§9.4), but **confirm is synchronous** — the server computes the SHA-256 inline and the document is `ready` immediately. **No polling.**

```ts
type DocumentOut = {
  id: string; dealId: string; docType: string; filename: string;
  contentType: string; sizeBytes: number | null; sha256: string | null;
  status: "pending" | "ready" | "failed";
  signatureStatus: "none" | "requested" | "signed" | "declined";
  uploadedBy: string | null; createdAt: string; updatedAt: string;
};
```

Download → `GET .../documents/{id}/download` — presigned, 15 min. **409 if not `ready`.**

Show `sha256` as an integrity indicator.

> **GAP (Low) —** `signatureStatus` exists but no e-signature provider is integrated; it is always `none` and there is no endpoint to request a signature. Render it read-only or hide it until a provider ships.

---

## 12.7 Content CMS — `/portal/content` 🔒 `content:manage`

**Marketing and admin only.** Team leads and agents have no access.

### Pages

`GET`/`POST /portal/content/pages` · `GET`/`PATCH`/`DELETE /portal/content/pages/{id}` · `POST .../publish` · `POST .../unpublish` · `POST .../preview-token`

```ts
type PageCreate = {
  slug: string;                          // ^[a-z0-9]+(-[a-z0-9]+)*$, ≤160
  title: Record<string, string>;         // required, ≤200 per locale
  blocks?: { type: BlockType; data: object }[];
  seoMeta?: { title?: string; description?: string; ogImage?: string };  // ogImage ≤500
  status?: "draft" | "published";
};
```

**The block editor is the single largest piece of frontend work in the portal.** The backend validates only `type` (one of nine) and that `data` is an object — the entire inner schema is yours to define and enforce. Build a per-type form, and keep it in lockstep with the public renderer (§10.12).

**Preview:** `POST .../preview-token` → a token for `/pages/{slug}/preview?token=` — shareable without an account. Show as a copyable link and explain it exposes the draft to anyone holding it.

**`publishedAt` is stamped on first publish and never reset** — unpublishing and republishing keeps the original date (it powers the sitemap `lastmod`).

**Slug collision → 409.**

### Legal pages — append-only versioning

`GET /portal/content/legal` · `POST /portal/content/legal` · `GET /portal/content/legal/{kind}/history`

```ts
{ kind: "privacy" | "terms" | "fair_treatment" | "license_disclosure";
  body: Record<string, string>;   // required, ≤100000 per locale
  effectiveAt?: string }
```

**Every publish creates a new immutable version.** There is no edit and no delete — this is the audit trail proving what a user consented to. Make it explicit in the UI: *"Publishing creates version {n+1}. Previous versions are kept permanently."*

The history view is a compliance feature — show version, effective date, and a diff if you can.

### Neighborhood guides

`GET`/`POST /portal/content/guides` · `GET`/`PATCH`/`DELETE /portal/content/guides/{id}` · `publish` / `unpublish`

```ts
{ slug: string;
  name: Record<string, string>;    // required
  body?: Record<string, string>;   // ≤100000
  boundary?: [number, number][][]; // ≤20 rings, ≤500 points each, [lon, lat]
  seoMeta?: SeoMeta;
  status?: PageStatus }
```

**Boundary drawing needs a map polygon tool.** Rings are `[lon, lat]`, auto-closed. A guide without a boundary is valid (pure editorial) but gets no auto-linked listings and no stats.

`stats` (listing count, median price) is **worker-computed nightly and never client-writable**. A new guide shows `{}` until the first sweep — say "Statistics will appear within 24 hours."

### Market reports

`GET`/`POST /portal/content/reports` · `PATCH`/`DELETE` · `publish` / `unpublish`

```ts
{ slug: string; title: Record<string, string>; stats: object }
```

**Publishing enqueues a PDF render.** Status flows `draft → published → ready`. Poll until `ready`; the public download gate 409s before then. Show "Generating PDF…" in the interim.

`stats` is author-supplied opaque JSON that becomes the PDF table — define and document your own shape.

---

## 12.8 Blog — `/portal/blog` 🔒 `content:manage`

`GET`/`POST /portal/blog/categories` · `PATCH`/`DELETE /portal/blog/categories/{id}`
`GET`/`POST /portal/blog/posts` · `GET`/`PATCH`/`DELETE /portal/blog/posts/{id}` · `publish` / `unpublish`

```ts
type PostCreate = {
  slug: string;
  title: Record<string, string>;      // required, ≤200
  excerpt?: Record<string, string>;   // ≤1000 — stored as plain text
  body: Record<string, string>;       // required, ≤200000 — rich text
  categoryId?: string;
  tags?: string[];                    // ≤20, each ≤40, lowercased + deduped server-side
  coverImage?: string;                // ≤500 — a URL, no upload pipeline
  seoMeta?: SeoMeta;
  status?: "draft" | "scheduled" | "published";
  scheduledAt?: string;
};
```

**Rich-text is sanitized server-side on write.** The stored allowlist is `p br strong em b i u ul ol li h2-h4 blockquote a img`; everything else is stripped, `javascript:` URLs are blocked, and links get `rel="noopener noreferrer nofollow"`.

**Configure your WYSIWYG to the same allowlist** — otherwise editors produce formatting that silently disappears on save, which reads as data loss.

**Scheduled publishing:** `status: "scheduled"` requires a **future** `scheduledAt` (422 otherwise). A sweep runs every 5 minutes, so a post goes live within ~5 min of its time.

**The subtle trap:** a PATCH that changes only `scheduledAt` while status stays `scheduled` is still validated — a past time is a **409**. Do not allow backdating a scheduled post.

`coverImage` is a plain URL — there is no upload pipeline for it.

**Deleting a category sets posts' `categoryId` to null; it does not delete the posts.** Say so in the confirmation.

---

## 12.9 Reviews — `/portal/reviews` 🔒 `review:moderate`

**Marketing and admin only.**

`GET /portal/reviews?status=&agentUserId=&cursor=&limit=` · `GET /portal/reviews/{id}` · `POST /portal/reviews/{id}/moderate` · `DELETE /portal/reviews/{id}`

```ts
type ReviewOut = {
  id: string; agentUserId: string | null; listingId: string | null;
  rating: number; title: string | null; body: string;
  authorName: string; authorEmail: string | null;
  status: "pending" | "approved" | "rejected";
  isVerified: boolean;
  moderatedBy: string | null; moderatedAt: string | null;
  moderationNote: string | null;
  createdAt: string; updatedAt: string;
};

// moderate
{ status: "approved" | "rejected";   // "pending" is rejected (422)
  isVerified?: boolean;
  note?: string }                     // ≤500
```

**Moderation is one-way.** Re-applying the *same* decision is idempotent (200), but flipping approved ↔ rejected is a **409**. Warn before the first decision: *"This cannot be changed later."*

Default the queue to `status=pending`. Show the pending count in the nav — it is a work queue.

Only approved reviews appear publicly. `agentUserId: null` means an agency-wide testimonial.

---

## 12.10 Analytics — `/portal/analytics`

**Two routers with different gates — this matters.**

### Aggregate dashboards 🔒 `analytics:view` (admin, team lead, marketing)

All take `start?` / `end?` (dates, default 30 days, max 366).

| Endpoint | Response |
|---|---|
| `GET /portal/analytics/traffic` | `{totalViews, totalSaves, totalInquiries, series: [{day, views, saves, inquiries}]}` |
| `GET /portal/analytics/top-listings` | `{listingId, views, saves, inquiries}[]` — plus `limit` (1–100, default 12) |
| `GET /portal/analytics/lead-funnel` | `{totalCreated, totalWon, totalLost, conversionRate, series: [{day, leadsCreated, leadsWon, leadsLost}]}` |
| `GET /portal/analytics/sources` | `{source, leadsCreated, leadsWon, conversionRate}[]` |

**These are tenant-wide aggregates** — not scoped. A team lead sees the whole agency's numbers here.

### Per-listing report — **authenticated only, no permission**

`GET /portal/analytics/listing-performance?start=&end=` → `{windowStart, windowEnd, listings: [{listingId, views, saves, inquiries}]}`

Deliberately on a separate router with no `analytics:view` gate — it is **visibility-scoped** to the actor's own listings, so ownership is the authorization. An agent sees their own numbers without agency-wide access. A user with no listings gets an allowed-but-empty 200.

### Rendering notes

- **Data comes from nightly rollups**, not live events. Yesterday and today are re-aggregated at 02:00, so today's numbers are near-current but not real-time. State the freshness on the page
- `conversionRate` is 0–1 — format as a percentage
- **The funnel is a cohort**: "of leads created on day X, how many are now won/lost." It is not a snapshot of current pipeline. Label it accordingly or it will be misread
- `topListings` and `listingPerformance` return only `listingId` — resolve titles via `GET /portal/listings/{id}` per row (N+1) or maintain a client-side map from the listings page
- Charts do not mirror in RTL (§6.3)

> **GAP (Low) —** No endpoint resolves a batch of listing ids to titles, so analytics tables need N+1 lookups. *Suggested: `GET /portal/listings?ids=`.*

---

## 12.11 Syndication — `/portal/syndication` 🔒 `listing:manage`

| Endpoint | Purpose |
|---|---|
| `GET`/`PUT /portal/syndication/settings` | Per-portal config |
| `GET /portal/syndication/state?portal=&cursor=&limit=` | Sync state across listings |
| `GET /portal/syndication/listings/{id}/state` | One listing's state |
| `POST /portal/syndication/listings/{id}/repush` | Manual retry → `{queued: string[]}` |

```ts
// GET returns EVERY known portal, configured or not
type PortalConfigOut = {
  key: string; enabled: boolean;
  baseUrl: string | null;
  hasApiKey: boolean;        // the key itself is NEVER returned
};

// PUT
{ portals: { [key: string]: { enabled?: boolean; baseUrl?: string; apiKey?: string } } }
```

**`apiKey` is write-only.** Render a password field showing "•••• configured" when `hasApiKey` is true. **Omitting `apiKey` on PUT preserves the stored key** — so an admin can toggle `enabled` without re-entering the secret. Send `apiKey` only when actually changing it.

Portal keys are validated against a server-side allowlist — an unknown key is a 422. `GET` returns all known portals, so render toggles from that response rather than hardcoding a list.

**Sync state:**
```ts
{ id, listingId, portalKey, remoteId: string | null,
  lastStatus: "pending" | "synced" | "removed" | "failed" | "paused",
  lastPushedAt: string | null, lastError: string | null,
  retryCount: number, consecutiveFailures: number, circuitOpen: boolean,
  updatedAt: string }
```

**`circuitOpen: true` is the state that needs UI attention.** After 5 consecutive failures the portal is paused and **no further syncs are attempted** until someone intervenes. Surface it prominently with `lastError` and a "Retry now" button — repush clears the breaker.

Sync is automatic on publish/edit/archive; there is nothing to trigger manually in normal operation.

**Feeds:** `/feeds/listings.xml` and `/feeds/listings.csv` are public, unauthenticated, live queries (≤5000). Show the URLs so an admin can hand them to a partner.

---

## 12.12 Webhooks — `/portal/webhooks` 🔒 `webhook:manage`

**Admin only.**

`GET`/`POST /portal/webhooks/endpoints` · `GET`/`PATCH`/`DELETE /portal/webhooks/endpoints/{id}` · `GET /portal/webhooks/deliveries?endpointId=&cursor=&limit=`

```ts
{ url: string;          // ≤2000
  events: string[];     // ≥1, from the allowlist
  description?: string }  // ≤200

// Subscribable events — exactly three:
"lead.created" | "listing.published" | "deal.closed"
```

**The signing secret is returned exactly once**, on create, in `WebhookEndpointCreatedOut.secret`. Every later read omits it entirely.

> Display it in a modal that cannot be dismissed accidentally, with copy-to-clipboard and: *"Copy this now — it will never be shown again."* There is no regenerate endpoint, so a lost secret means deleting and recreating the endpoint.

**URL validation is strict (SSRF protection):** private, loopback, link-local and reserved addresses are rejected with **422 `invalid-webhook-url`**. Copy: *"This URL must be a publicly reachable https address."*

**Deliveries:**
```ts
{ id, endpointId, eventType, status, attempts,
  responseStatus: number | null, lastError: string | null,
  deliveredAt: string | null, createdAt: string }
```

Same circuit breaker as syndication — 5 consecutive failures opens it and stops delivery. Surface `circuitOpen` on the endpoint and offer re-enable via PATCH.

Document the signature scheme for integrators: header `t=<unix>,v1=<hmac-sha256>`.

---

## 12.13 Users — `/portal/users` 🔒 `user:view` / `user:manage`

**Admin only.**

| Action | Endpoint | Gate |
|---|---|---|
| List | `GET /users` | `user:view` |
| Get | `GET /users/{id}` | `user:view` |
| Create | `POST /users` | `user:manage` |
| Update | `PATCH /users/{id}` | `user:manage` |
| Delete | `DELETE /users/{id}` | `user:manage` |

```ts
type UserCreate = {
  email: string; password: string;   // 8–128
  role: Role;                        // buyer_renter|seller|agent|team_lead|admin|marketing
  firstName?: string; lastName?: string; locale?: string; phone?: string;
};
type UserAdminUpdate = {
  role?: Role; status?: "active" | "disabled";
  firstName?: string; lastName?: string; locale?: string; phone?: string;
};
```

**Platform roles are never assignable here** (422).

**Changing a role or status, or deleting a user, force-logs-out that user immediately** — every live token is revoked, not just future ones. Warn: *"This will sign {name} out of all devices immediately."*

Delete is a soft delete.

---

## 12.14 Compliance — `/portal/compliance` 🔒 `compliance:manage`

**Admin only.**

### Cookie banner config

`GET` / `PUT /portal/compliance/cookie-config`

```ts
{ categories: object[];     // frontend-defined shape, envelope-validated only
  bannerCopy: object;       // i18n copy
  isEnabled: boolean }
```

You define the category and copy shape; the backend stores it opaquely. Build the editor and the public banner (§10.19) against the same contract.

### Audit log

`GET /portal/compliance/audit-log?action=&cursor=&limit=`

```ts
{ id, tenantId: string | null, actorUserId: string | null, actorRole: string | null,
  action: string, target: string | null, metadata: object,
  ip: string | null, createdAt: string }
```

**Pinned to this tenant** — an agency admin never sees another agency's or platform-only rows. This is the §10.11 audit-access report; present it as a read-only, filterable table with real pagination counts.

---

# PART V — PLATFORM BACK-OFFICE

**Route group:** `/platform/*` · **Auth:** platform token · **`noindex`**

This surface is **tenant-exempt** — it is not served from an agency domain and does not resolve a tenant from `Host`. Host it on your own operations domain.

Two roles: `platform_support` (view only) and `platform_admin` (full).

| Permission | Support | Admin |
|---|:---:|:---:|
| `platform:tenant:view` | ✅ | ✅ |
| `platform:tenant:manage` | — | ✅ |
| `platform:staff:manage` | — | ✅ |

## 13.1 Platform login — `/platform/login`

`POST /platform/auth/login` · `POST /platform/auth/mfa/verify` · `POST /platform/auth/refresh` · `POST /platform/auth/logout`

Same request/response contract as tenant auth (§4.2), but the **refresh cookie path is `/api/v1/platform/auth`**. Keep the two token stores separate in the client — a staff member may legitimately hold both.

**No self-registration.** Platform accounts are created by an existing platform admin.

**No per-endpoint rate limit** on platform auth (only the global per-IP budget applies), so implement a client-side attempt delay.

---

## 13.2 Tenants — `/platform/tenants` 🔒 `platform:tenant:view`

`GET /platform/tenants?cursor=&limit=` → `Page<TenantOut>` · `GET /platform/tenants/{id}`

```ts
type TenantOut = {
  id: string; name: string; slug: string;
  status: string;                       // trial | active | suspended
  plan: string;
  trialEndsAt: string | null;
  offboardingAt: string | null;
  deletionScheduledAt: string | null;
  settings: Record<string, unknown>;    // full blob, unredacted
  createdAt: string; updatedAt: string;
  domains: TenantDomainOut[];
};
```

**Note:** unlike `GET /site/config`, this returns the **full unredacted settings blob** including integration credentials. Treat the tenant detail page as sensitive; do not log it client-side.

**Warning surfaces to build:** trials expiring within 3 days, tenants with `offboardingAt` set (and their `deletionScheduledAt` countdown), suspended tenants.

### Create 🔒 `platform:tenant:manage`

```ts
{ name: string;      // ≤120
  slug: string;      // 2–63, ^[a-z0-9](-[a-z0-9])*$
  domain: string;
  settings?: object }
```

Creating a tenant starts a **14-day trial** and mints a DNS verification token. Slug/domain collision → 409.

### Lifecycle 🔒 `platform:tenant:manage`

| Action | Endpoint | Effect |
|---|---|---|
| Update | `PATCH /platform/tenants/{id}` | Name, settings |
| Suspend | `POST .../suspend` | Tenant serves **402** immediately on every request |
| Activate | `POST .../activate` | Restore |
| Set plan | `PUT .../plan` | Changes quota limits at once |
| Offboard | `POST .../offboard` | Suspend + schedule deletion (30 days) + export |
| Cancel offboard | `POST .../offboard/cancel` | Reactivate before purge |

**Suspension is instantaneous and total** — the agency's public website goes dark for its visitors, not just its staff. Require a typed confirmation.

**Offboarding is the most destructive action in the system.** It suspends the tenant, schedules a hard delete (all data, CASCADE), and enqueues a full data export. Require typing the tenant slug to confirm, show the exact deletion date, and make "Cancel offboard" prominent while `offboardingAt` is set.

### Domains 🔒 `platform:tenant:manage`

`POST /platform/tenants/{id}/domains` · `DELETE .../domains/{domainId}` · `POST .../domains/{domainId}/verify`

```ts
type TenantDomainOut = {
  id: string; domain: string; isPrimary: boolean;
  verificationToken: string | null;
  verificationStatus: string;           // pending | verified | failed
  verifiedAt: string | null;
};
```

**Verification is a DNS TXT challenge.** Show the exact record to create (name + `verificationToken` value) with copy-to-clipboard, then a "Verify now" button. A daily sweep also re-checks automatically.

**Important nuance:** an unverified domain **still serves traffic** — verification gates TLS certificate issuance, not routing. Do not present it as "the site is down until verified."

### Billing 🔒 mixed

| Endpoint | Gate |
|---|---|
| `GET /platform/tenants/{id}/subscription` | view |
| `POST /platform/tenants/{id}/checkout` | manage — **supports `Idempotency-Key`** |

```ts
type SubscriptionOut = {
  id: string; provider: string; plan: string;
  status: string;                       // active | past_due | canceled
  currentPeriodEnd: string | null;
  graceUntil: string | null;            // dunning window
  cancelAtPeriodEnd: boolean;
};
// checkout
{ plan: string; customerEmail: string }  → { url: string; sessionId: string }
```

Redirect to `url`. **Always send an `Idempotency-Key`** — this is money.

`status: "past_due"` with a `graceUntil` means dunning is running; the tenant auto-suspends when the window passes. Surface the countdown.

**The billing provider is currently a stub** — checkout does not charge. Label the UI accordingly in non-production, or you will confuse operators.

### Impersonation 🔒 `platform:tenant:manage`

`POST /platform/tenants/{id}/impersonate`

```ts
{ accessToken: string; tokenType: "bearer"; expiresIn: number;
  impersonation: true; tenantId: string; tenantSlug: string;
  actingAsUserId: string }
```

**Properties the UI must respect:**
- **15 minutes, no refresh token.** The session dies at expiry and cannot be renewed. On 401, return to the platform console — never show a login form
- **Audit-logged.** Every use is recorded
- `impersonation: true` is the explicit signal to render the banner (§12.1)

Open the impersonated session in a **new tab** so the operator does not lose their platform context, and require a reason field if your process demands one (note: the API does not currently accept one).

> **GAP (Low) —** Impersonation always targets the tenant's *first admin*; you cannot choose a specific user or role. Debugging an agent-specific issue is not possible this way.

---

## 13.3 Metrics — `/platform/metrics` 🔒 `platform:tenant:view`

`GET /platform/metrics`

```ts
{ totalTenants: number; activeTenants: number;
  trialTenants: number; suspendedTenants: number;
  totalListings: number; totalAgents: number;
  tenants: TenantMetricRow[] }
```

A live snapshot from running counters (not the analytics rollups), so it is cheap and current. Render as headline tiles plus a sortable per-tenant table — useful for spotting a tenant near its quota.

---

## 13.4 Audit log — `/platform/audit` 🔒 `platform:tenant:view`

`GET /platform/audit-log?tenantId=&action=&cursor=&limit=`

Same row shape as §12.14, but **cross-tenant** — a superset of the tenant-scoped report. Filter by tenant and action; `totalEstimate` is a real filtered count, so proper pagination is possible here.

---

## 13.5 Staff — `/platform/staff` 🔒 `platform:staff:manage`

`GET /platform/staff` · `POST /platform/staff`

```ts
{ email: string; password: string; role: "platform_admin" | "platform_support";
  firstName?: string; lastName?: string; locale?: string; phone?: string }
```

Only these two roles are accepted (422 otherwise).

> **GAP (Low) —** There is no update or delete for platform staff — only list and create. A staff member cannot be disabled or have their role changed through the API. *Suggested: `PATCH`/`DELETE /platform/staff/{id}`.*

---

# APPENDICES

## Appendix A — Endpoint index

**182 paths.** All relative to `/api/v1`.

### Public — anonymous

| Method | Path |
|---|---|
| GET | `/site/config` · `/site/cookie-config` |
| GET | `/listings` · `/listings/map` · `/listings/{refOrId}` |
| GET | `/sitemap.xml` |
| GET | `/agents` · `/agents/{slug}` · `/agents/{slug}/reviews` · `/agents/{slug}/slots` |
| POST | `/agents/{slug}/appointments` ⟳ |
| GET | `/appointments/ical/{token}` |
| POST | `/leads/capture` ⟳ · `/leads/capture/whatsapp-click` |
| POST | `/valuations` · `/tools/mortgage-estimate` · `/tools/mortgage-estimate/email` |
| PATCH | `/valuations/{token}` |
| POST | `/valuations/{token}/complete` |
| GET | `/pages/{slug}` · `/pages/{slug}/preview` · `/legal` · `/legal/{kind}` |
| GET | `/guides` · `/guides/{slug}` · `/reports/{slug}` |
| POST | `/reports/{slug}/download` |
| GET | `/blog/posts` · `/blog/posts/{slug}` · `/blog/categories` · `/blog/rss.xml` |
| GET | `/reviews` · `/reviews/summary` |
| POST | `/reviews` |
| POST | `/saved-searches` · `/saved-searches/confirm` · `/saved-searches/unsubscribe` |
| POST | `/consent` · `/analytics/events` |
| GET | `/feeds/listings.{fmt}` |
| POST | `/billing/webhook` (signature-verified) |

⟳ = supports `Idempotency-Key`

### Auth

`POST /auth/{register,login,mfa/verify,refresh,logout,logout-all}` · `POST /auth/password/{forgot,reset}` · `POST /auth/verify-email` · `POST /auth/verify-email/request` · `GET /auth/mfa/status` · `POST /auth/mfa/{enrol,enrol/confirm,disable}` · `GET`/`DELETE /auth/sessions[/{id}]` · `GET /auth/oauth/providers` · `POST /auth/oauth/{provider}/{start,callback}`

### Buyer `/me` — 👤 ownership

`GET`/`PATCH /users/me` · `PUT`/`DELETE /me/favorites/{listingId}` · `GET /me/favorites` · `GET`/`POST /me/saved-searches` · `GET`/`PATCH`/`DELETE /me/saved-searches/{id}` · **`GET /me/appointments`** · `GET /me/notifications` · `GET /me/notifications/unread-count` · `POST /me/notifications/{mark-read,ws-ticket}` · `GET`/`PUT /me/notifications/preferences` · `WS /ws/notifications` · `GET /me/export` · `DELETE /me` · `GET /me/dsr/{id}`

### Portal — by permission

| Permission | Paths |
|---|---|
| `listing:manage` | `/portal/listings*`, `/portal/media/*`, `/portal/syndication/*` |
| `lead:manage` | `/portal/leads*` |
| `lead:view_all` | `/portal/contacts/{id}*` |
| `lead:assign` | `/portal/leads/assignment-rule` |
| `agent:manage` | `/portal/agents` (roster), `/portal/teams*` |
| 👤 own-or-manager | `/portal/agents/{id}*`, `/portal/agents/{id}/availability`, `/ical` |
| `appointment:manage` | `/portal/appointments*` |
| `deal:manage` | `/portal/deals*` (commission sub-routes: **admin only**) |
| `content:manage` | `/portal/content/*`, `/portal/blog/*` |
| `review:moderate` | `/portal/reviews*` |
| `analytics:view` | `/portal/analytics/{traffic,top-listings,lead-funnel,sources}` |
| 👤 authenticated | `/portal/analytics/listing-performance` |
| `webhook:manage` | `/portal/webhooks/*` |
| `compliance:manage` | `/portal/compliance/*` |
| `user:view` / `user:manage` | `/users*` |

### Platform

`POST /platform/auth/{login,mfa/verify,refresh,logout}` · `GET`/`POST /platform/tenants` · `GET`/`PATCH /platform/tenants/{id}` · `POST /platform/tenants/{id}/{suspend,activate,offboard,offboard/cancel,impersonate,checkout⟳}` · `PUT /platform/tenants/{id}/plan` · `GET /platform/tenants/{id}/subscription` · `POST`/`DELETE /platform/tenants/{id}/domains[/{domainId}]` · `POST .../domains/{domainId}/verify` · `GET /platform/metrics` · `GET /platform/audit-log` · `GET`/`POST /platform/staff`

---

## Appendix B — Enums (exact wire values)

```ts
type Role = "buyer_renter" | "seller" | "agent" | "team_lead" | "marketing"
          | "admin" | "platform_admin" | "platform_support";

type ListingStatus = "draft" | "review" | "published" | "reserved"
                   | "sold" | "rented" | "archived";
type ListingPurpose = "sale" | "rent" | "rent_daily";
type PricePeriod = "month" | "day";
type PortalSort = "newest" | "updated" | "price_asc" | "price_desc";
type SearchSort  = "newest" | "price_asc" | "price_desc" | "area_asc" | "area_desc";

type PropertyType = "apartment" | "house" | "villa" | "studio" | "duplex" | "land"
                  | "office" | "retail" | "warehouse" | "garage" | "farm"
                  | "building" | "other";

// 18 values — the only accepted listing features
type ListingFeature =
  | "air_conditioning" | "balcony" | "basement" | "elevator" | "equipped_kitchen"
  | "fiber_internet" | "furnished" | "garage" | "garden" | "heating"
  | "mountain_view" | "parking" | "pool" | "sea_view" | "security"
  | "solar_panels" | "terrace" | "wheelchair_access";

// 10 values
type AgentSpecialty =
  | "residential_sales" | "residential_rentals" | "commercial" | "luxury" | "land"
  | "new_developments" | "off_plan" | "property_management" | "valuation" | "industrial";

type LeadSource = "listing_form" | "valuation" | "mortgage" | "market_report"
                | "search_signup" | "chat" | "whatsapp_click" | "tour_request"
                | "phone" | "portal" | "ad" | "other";
type LeadStage = "new" | "contacted" | "qualified" | "touring" | "offer" | "won" | "lost";
type ActivityType = "note" | "call" | "email" | "sms" | "status_change"
                  | "assignment" | "tour" | "no_show" | "system";
// client-loggable subset: note | call | email | sms | tour
type AssignmentStrategy = "listing_agent" | "round_robin" | "territory";

type AppointmentStatus = "requested" | "confirmed" | "completed" | "cancelled" | "no_show";

type DealStatus = "open" | "under_contract" | "closed_won" | "closed_lost";
type DealDocumentStatus = "pending" | "ready" | "failed";
type SignatureStatus = "none" | "requested" | "signed" | "declined";
type CommissionBasis = "percentage" | "flat";

type MediaKind = "photo" | "floorplan" | "doc" | "video" | "tour_3d";
type MediaStatus = "pending" | "processing" | "ready" | "failed";

type PageStatus = "draft" | "published";
type ReportStatus = "draft" | "published" | "ready";
type PostStatus = "draft" | "scheduled" | "published";
type LegalKind = "privacy" | "terms" | "fair_treatment" | "license_disclosure";
type BlockType = "hero" | "richtext" | "listings_grid" | "cta" | "image"
               | "gallery" | "faq" | "stats" | "contact";

type ReviewStatus = "pending" | "approved" | "rejected";
type AlertFrequency = "instant" | "daily" | "weekly";

type NotificationType = "lead_assigned" | "lead_escalated" | "appointment_reminder"
                      | "appointment_confirmed" | "appointment_cancelled" | "milestone_due";
type NotificationChannel = "in_app" | "email" | "sms" | "whatsapp";

type EventType = "listing_view" | "search" | "favorite"
               | "form_start" | "form_submit" | "page_view";

type ConsentCategory = "necessary" | "analytics" | "marketing";
type DsrKind = "export" | "erasure";
type DsrStatus = "pending" | "completed" | "cancelled";

type SyncStatus = "pending" | "synced" | "removed" | "failed" | "paused";
type WebhookEvent = "lead.created" | "listing.published" | "deal.closed";
```

---

## Appendix C — Error catalogue

Branch on the **slug** (last segment of `problem.type`).

| Slug | HTTP | Meaning | Suggested copy |
|---|---|---|---|
| `not-found` | 404 | Missing **or** out of scope | "Not found, or you don't have access to it." |
| `conflict` | 409 | Illegal transition, duplicate, state clash | Context-specific — see below |
| `permission-denied` | 403 | Missing permission | "You don't have permission to do that." |
| `unauthorized` | 401 | No/expired/revoked token | Trigger refresh; on failure, sign out |
| `quota-exceeded` | 403 | Plan limit reached | "You've reached your plan's limit of {n} {resource}." |
| `tenant-suspended` | 402 | Agency suspended | Full-page maintenance screen |
| `rate-limited` | 429 | Too many requests | "Too many attempts. Try again in {retryAfter}s." |
| `validation-error` | 422 | Body/params invalid | Map `errors[].loc[1]` to fields |
| `breached-password` | 422 | Password in a breach corpus | "This password appeared in a data breach. Choose another." |
| `feature-not-configured` | 501 | Not enabled on this deployment | Hide the feature |
| `invalid-webhook-url` | 422 | SSRF-blocked URL | "Must be a publicly reachable https address." |
| `idempotency-key-in-flight` | 409 | Duplicate still processing | "Still processing your previous request." |
| `upstream-unavailable` | 503 | Third party failed (AI) | "Temporarily unavailable. Please try again." |
| `invalid-cursor` | 400 | Malformed/sort-mismatched cursor | Reset to page 1 silently |
| `internal-error` | 500 | Server fault | "Something went wrong." + `requestId` |

**409 needs contextual copy** — it is the most overloaded:

| Where | Meaning |
|---|---|
| Listing transition | Illegal status change |
| Listing delete | Must archive first |
| Tour booking | Slot taken, or not a valid slot start |
| Deal `closed_lost` | Missing `lostReason` |
| Review moderation | Already decided the other way |
| Saved search create | 20-search limit |
| Slug fields | Already in use |
| Report download | PDF not ready |
| Blog scheduled | `scheduledAt` in the past |
| Consent | Anonymous submission with no `sessionId` |
| WhatsApp click | No number configured |

---

## Appendix D — Gap register

Ordered by impact. Every item is a *missing* capability, not a defect.

### Medium — blocks or degrades a normal user journey

| # | Gap | Impact | Suggested change |
|---|---|---|---|
| D1 | **Lead list has no contact details.** `LeadOut` carries only `contactId`; the contact endpoint is single-fetch and needs `lead:view_all`, which agents lack | An agent cannot see who their own lead is from the inbox. The most-used portal screen is unusable without N+1 fetches | Embed `contact: {firstName, lastName, email, phone}` in `LeadOut`, or add `GET /portal/contacts?ids=` |
| D2 | **Appointment list has no contact details.** Same root cause | An agent cannot see who they are meeting | Embed a contact summary in `AppointmentOut` |
| D3 | **No buyer-side tour cancel.** Only staff can transition an appointment | A visitor who cannot attend has no way to say so → avoidable no-shows, which the system then penalises in the lead score | `POST /me/appointments/{id}/cancel`, same verified-email ownership join, `requested\|confirmed → cancelled` only |
| D4 | **Public listing detail has no agent.** No `agentId`, name, slug or photo | Cannot build "contact this agent" on the highest-intent page. Lead routing still works, but the visitor sees nobody | Add `agent: {slug, displayName, photoVariants} \| null` to the detail response |
| D5 | **No authenticated consent surface.** `POST /consent` is anonymous/session-keyed; no `GET /me/consent` | A signed-in user cannot see or change what they agreed to — a GDPR-adjacent expectation | `GET /me/consent` + authenticated `POST /me/consent` |
| D6 | **Analytics ingestion is anonymous-only.** No optional-auth | Logged-in behaviour cannot be attributed; no personalisation, no per-user reporting | Optional-auth dependency on `POST /analytics/events` |
| D7 | **SMS/WhatsApp channels have no adapter.** Enabling logs `skipped` | Preference toggles that silently do nothing | Ship an adapter, or have the API mark unavailable channels so the UI can disable them from data |

### Low — workaroundable or cosmetic

| # | Gap | Impact | Suggested change |
|---|---|---|---|
| D8 | No contact **list** endpoint (get-by-id only) | No browse-all-contacts page | `GET /portal/contacts?q=&cursor=` |
| D9 | No batch listing lookup | Analytics tables N+1 to resolve titles | `GET /portal/listings?ids=` |
| D10 | No password-change for a signed-in user | Must use the email reset flow | `POST /auth/password/change` |
| D11 | No cancel for a pending erasure | The 30-day countdown cannot be stopped | `POST /me/dsr/{id}/cancel` |
| D12 | `GET /me/export` creates no DSR row | Exports are not trackable in DSR history | Record an export DSR |
| D13 | All 6 notification types are staff-facing | The buyer notification centre is permanently empty | Add `tour_confirmed`, `saved_search_match` |
| D14 | `digestEligible` false for every type | Quiet-hours has nothing to affect | — (build the UI when a type qualifies) |
| D15 | No JSON-LD for agents, posts, organisation | Weaker structured data than listings | Extend the backend, or build frontend-side |
| D16 | Media reorder is per-item PATCH | Drag-drop fires N requests | Bulk reorder endpoint |
| D17 | `signatureStatus` with no provider | Always `none`, no action possible | Integrate a provider or hide |
| D18 | No platform-staff update/delete | Staff cannot be disabled via API | `PATCH`/`DELETE /platform/staff/{id}` |
| D19 | Impersonation targets the first admin only | Cannot debug an agent-specific issue | Accept a target user id |
| D20 | Tenant `settings` has no schema | Branding/theme shape is undefined | Frontend defines and documents it |
| D21 | Listing documents are private-bucket only | No public brochure download | (Deliberate) |
| D22 | `MyAppointmentOut.agentUserId` has no name/slug | Buyer cannot see who they are meeting | Embed a minimal agent summary |

**None of these block starting the frontend.** D1 and D2 are the two to raise first — they affect the portal's most-used screens.

---

## Appendix E — Permission → UI capability

| Capability | AG | TL | MK | A |
|---|:--:|:--:|:--:|:--:|
| See own listings | ✅ | ✅ | ✅ | ✅ |
| See team listings | — | ✅ | ✅ | ✅ |
| See all listings | — | — | ✅ | ✅ |
| Create/edit listings | ✅ | ✅ | ✅ | ✅ |
| Publish a listing | ⚠️¹ | ✅ | ⚠️¹ | ✅ |
| Assign a listing to another agent | — | ✅ | ✅ | ✅ |
| Set `featured` | — | ✅ | ✅ | ✅ |
| See own leads | ✅ | ✅ | ✅ | ✅ |
| See all leads | — | ✅ | ✅ | ✅ |
| Reassign a lead | — | ✅ | ✅ | ✅ |
| Configure assignment rules | — | ✅ | ✅ | ✅ |
| View/edit contacts | — | ✅ | ✅ | ✅ |
| Manage own agent profile | ✅ | ✅ | ✅ | ✅ |
| Publish an agent profile | — | ✅ | ✅ | ✅ |
| Manage teams | — | ✅ | ✅ | ✅ |
| See own tours | ✅ | ✅ | ✅ | ✅ |
| Transition a tour | ✅ | ✅ | ✅ | ✅ |
| See/manage deals | ✅ | ✅ | **—** | ✅ |
| **See commission figures** | — | — | — | ✅ |
| Manage CMS/blog | — | — | ✅ | ✅ |
| Moderate reviews | — | — | ✅ | ✅ |
| Aggregate analytics | — | ✅ | ✅ | ✅ |
| Own-listing analytics | ✅ | ✅ | ✅ | ✅ |
| Syndication settings | ✅ | ✅ | ✅ | ✅ |
| Webhooks | — | — | — | ✅ |
| Manage users | — | — | — | ✅ |
| Compliance | — | — | — | ✅ |

¹ Only when `settings.listings.agent_self_publish` is enabled.

---

## Appendix F — Build sequence

Ordered so each phase is independently demonstrable.

**Phase 1 — Foundation**
API client, error handling, auth flows, tenant bootstrap, i18n + RTL, design system, layout shell. *Nothing is demoable, and everything depends on it.*

**Phase 2 — Public site (buyer-facing value)**
Home, listing search, listing detail, map, agent directory + profile, CMS pages, blog, legal. *This is the agency's shop window and the first thing to show a customer.*

**Phase 3 — Lead generation (revenue)**
Lead capture forms, WhatsApp handoff, tour booking, valuation wizard, mortgage tools, review submission, saved-search signup, cookie consent + analytics. *This is what the product is actually for.*

**Phase 4 — Agency portal core**
Portal shell + role gating, listings CRUD + media + workflow, leads inbox + detail, tours agenda + availability. *Raise gaps D1/D2 before starting the leads inbox.*

**Phase 5 — Buyer account**
Auth pages, favorites, saved searches, my tours, notifications + WebSocket, profile, security, privacy.

**Phase 6 — Portal completion**
Agents + teams, deals + milestones + documents, CMS editor (the block editor is the big one), blog editor, reviews moderation, analytics dashboards.

**Phase 7 — Admin & platform**
Users, syndication, webhooks, compliance, then the full platform back-office.

**Cross-cutting, throughout:** accessibility (keyboard, focus, contrast, screen readers), RTL verification on every screen, empty/loading/error states, and testing the four states of every data view.

---

*End of specification. Verified against the implementation on 2026-08-04.*
