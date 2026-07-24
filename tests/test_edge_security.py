"""Edge hardening (§10.1 / §10.2): security headers, tenant-aware CORS, and
the layered rate limits."""

from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.types import Receive, Scope, Send

from app.core.config import Settings, get_settings
from app.core.cors import TenantCORSMiddleware, origin_host, strip_port
from app.core.middleware import API_CSP, DOCS_CSP, SecurityHeadersMiddleware
from app.main import create_app
from tests.helpers import HOST_A, HOST_B
from tests.test_tenants_platform_api import create_tenant

ORIGIN_A = f"http://{HOST_A}"
ORIGIN_B = f"http://{HOST_B}"


async def create_two_tenants(client: AsyncClient, headers: dict[str, str]) -> None:
    await create_tenant(client, headers)
    await create_tenant(client, headers, name="Agency B", slug="agency-b", domain=HOST_B)


# ---------------------------------------------------------------- headers ---


async def test_baseline_security_headers_present(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"


async def test_api_csp_is_default_deny(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.headers["content-security-policy"] == API_CSP
    assert "default-src 'none'" in resp.headers["content-security-policy"]


async def test_docs_get_the_swagger_csp(client: AsyncClient) -> None:
    """/docs serves HTML, so it needs the looser (but still allowlisted) policy
    — everything else stays default-deny."""
    resp = await client.get("/docs")
    assert resp.status_code == 200
    assert resp.headers["content-security-policy"] == DOCS_CSP
    assert "cdn.jsdelivr.net" in resp.headers["content-security-policy"]


async def test_no_hsts_on_plain_http_local(client: AsyncClient) -> None:
    """A max-age cached from http://localhost would pin the developer's browser
    to https for a year."""
    resp = await client.get("/healthz")
    assert "strict-transport-security" not in resp.headers


async def test_hsts_present_over_https_even_locally(client: AsyncClient) -> None:
    resp = await client.get("https://testserver/healthz")
    assert resp.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


async def _noop_app(scope: Scope, receive: Receive, send: Send) -> None:
    """A stand-in inner app: these tests exercise header construction only."""


def test_hsts_emitted_for_tls_deployments() -> None:
    """A staging/production deployment sends HSTS even though TLS terminates at
    Caddy, so the app's own header survives a proxy misconfiguration."""
    base = get_settings().model_dump()
    scope: Scope = {"type": "http", "path": "/api/v1/site/config", "scheme": "http"}

    prod = SecurityHeadersMiddleware(_noop_app, Settings(**{**base, "app_env": "production"}))
    assert any(k == b"strict-transport-security" for k, _ in prod._headers_for(scope))

    local = SecurityHeadersMiddleware(_noop_app, Settings(**base))
    assert not any(k == b"strict-transport-security" for k, _ in local._headers_for(scope))


def test_hsts_subdomain_directive_is_configurable() -> None:
    base = get_settings().model_dump()
    settings = Settings(**{**base, "app_env": "production", "hsts_include_subdomains": False})
    mw = SecurityHeadersMiddleware(_noop_app, settings)
    scope: Scope = {"type": "http", "path": "/", "scheme": "https"}
    assert dict(mw._headers_for(scope))[b"strict-transport-security"] == b"max-age=31536000"


# ------------------------------------------------------------------- CORS ---


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("http://agency-a.test", "agency-a.test"),
        ("https://agency-a.test:8443", "agency-a.test"),
        ("https://AGENCY-A.test", "agency-a.test"),
        ("http://[::1]:3000", "[::1]"),
        ("null", ""),
        ("", ""),
    ],
)
def test_origin_host_parsing(origin: str, expected: str) -> None:
    assert origin_host(origin) == expected


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("agency-a.test", "agency-a.test"),
        ("agency-a.test:8443", "agency-a.test"),
        ("AGENCY-A.test", "agency-a.test"),
        # A bare split(":") would mangle these to "[" and "[2001".
        ("[::1]:8000", "[::1]"),
        ("[2001:db8::1]", "[2001:db8::1]"),
        # Unterminated bracket: unparseable, so it must not match anything.
        ("[bad", ""),
        ("", ""),
    ],
)
def test_host_port_stripping(host: str, expected: str) -> None:
    """The ``Host`` parse must agree with the ``Origin`` parse on what host a
    value names — including IPv6 literals."""
    assert strip_port(host) == expected


async def test_same_ipv6_host_is_allowed() -> None:
    """An IPv6-literal deployment is same-origin with itself. Parsing ``Host``
    with a bare ``split(":")`` yields ``"["`` and silently denies every such
    request, so this asserts the two parses agree."""

    async def noop(scope: Scope, receive: Receive, send: Send) -> None: ...

    middleware = TenantCORSMiddleware(noop, get_settings())
    scope: Scope = {"type": "http", "headers": [(b"host", b"[::1]:8000")], "app": None}
    assert await middleware._is_allowed(scope, "http://[::1]:3000") is True
    # A *different* IPv6 host must still be refused — the fix must not widen.
    assert await middleware._is_allowed(scope, "http://[::2]:3000") is False


async def test_cors_reflects_the_tenants_own_domain(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await create_two_tenants(client, platform_headers)
    resp = await client.get("/api/v1/site/config", headers={"Host": HOST_A, "Origin": ORIGIN_A})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ORIGIN_A
    assert resp.headers["access-control-allow-credentials"] == "true"
    # A shared cache must not hand agency A's response to another origin.
    assert "Origin" in resp.headers["vary"]


async def test_cors_never_reflects_another_tenants_domain(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    """The whole point of resolving per request: agency B's site is a perfectly
    valid tenant domain, and still must not be allowed to read agency A's API
    with credentials."""
    await create_two_tenants(client, platform_headers)
    resp = await client.get("/api/v1/site/config", headers={"Host": HOST_A, "Origin": ORIGIN_B})
    # The request itself still succeeds — the browser is what enforces CORS.
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


async def test_cors_rejects_an_unknown_origin(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await create_tenant(client, platform_headers)
    resp = await client.get(
        "/api/v1/site/config",
        headers={"Host": HOST_A, "Origin": "https://evil.example.com"},
    )
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


async def test_preflight_is_answered_for_an_allowed_origin(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await create_tenant(client, platform_headers)
    resp = await client.request(
        "OPTIONS",
        "/api/v1/auth/login",
        headers={
            "Host": HOST_A,
            "Origin": ORIGIN_A,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == ORIGIN_A
    assert "POST" in resp.headers["access-control-allow-methods"]
    assert "Authorization" in resp.headers["access-control-allow-headers"]


async def test_preflight_rejected_for_a_foreign_origin(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await create_two_tenants(client, platform_headers)
    resp = await client.request(
        "OPTIONS",
        "/api/v1/auth/login",
        headers={
            "Host": HOST_A,
            "Origin": ORIGIN_B,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.status_code == 403
    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.parametrize("origin", [ORIGIN_A, ORIGIN_B])
async def test_preflight_still_carries_security_headers(
    client: AsyncClient, platform_headers: dict[str, str], origin: str
) -> None:
    """CORS answers a preflight itself, *outside* SecurityHeadersMiddleware, so
    without explicit stamping it would be the one response in the app carrying
    no security headers — on both the allowed and the rejected path."""
    await create_two_tenants(client, platform_headers)
    resp = await client.request(
        "OPTIONS",
        "/api/v1/auth/login",
        headers={
            "Host": HOST_A,
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert resp.headers["content-security-policy"] == API_CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"


async def test_cors_never_returns_a_wildcard(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    """`*` with credentials is exactly the combination that would make every
    agency site readable by any page on the internet."""
    await create_tenant(client, platform_headers)
    resp = await client.get("/api/v1/site/config", headers={"Host": HOST_A, "Origin": ORIGIN_A})
    assert resp.headers["access-control-allow-origin"] != "*"


async def test_static_platform_allowlist_still_works(
    platform_headers: dict[str, str],
) -> None:
    """The back-office SPA belongs to no tenant, so it stays an env-configured
    additive entry."""
    get_settings.cache_clear()
    settings = Settings(**{**get_settings().model_dump(), "cors_origins": "https://admin.example"})
    application = create_app(settings)
    # No lifespan needed: the static branch never touches the resolver.
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/healthz", headers={"Origin": "https://admin.example"})
    assert resp.headers["access-control-allow-origin"] == "https://admin.example"
    get_settings.cache_clear()


async def test_cors_denies_rather_than_raising_without_a_resolver() -> None:
    """CORS runs above tenant resolution, so it reaches for the resolver
    itself. Anything wrong there (an outage, or an app built with no lifespan)
    must deny the origin, never raise out of middleware into a 500."""
    application = create_app(get_settings())
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/healthz", headers={"Origin": "https://unknown.example"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


async def test_request_without_origin_is_untouched(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


# ------------------------------------------------------------ rate limits ---


async def test_login_rate_limits_after_the_budget(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await create_tenant(client, platform_headers)
    limit = get_settings().auth_rate_limit_per_minute

    statuses = []
    for _ in range(limit + 2):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "Wrong-Pass-123456"},
            headers={"Host": HOST_A},
        )
        statuses.append(resp.status_code)

    assert statuses[0] == 401, "a wrong password is still a plain 401 while in budget"
    assert 429 in statuses, f"login was never limited: {statuses}"

    limited = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "Wrong-Pass-123456"},
        headers={"Host": HOST_A},
    )
    assert limited.status_code == 429
    assert limited.headers["content-type"].startswith("application/problem+json")
    assert limited.json()["type"].endswith("rate-limited")
    # §10.2 requires the standard header, not just a body field.
    assert int(limited.headers["Retry-After"]) > 0


async def test_auth_actions_have_separate_budgets(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    """Exhausting login must not lock the same caller out of password reset."""
    await create_tenant(client, platform_headers)
    limit = get_settings().auth_rate_limit_per_minute
    for _ in range(limit + 2):
        await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "Wrong-Pass-123456"},
            headers={"Host": HOST_A},
        )

    resp = await client.post(
        "/api/v1/auth/password/forgot",
        json={"email": "nobody@example.com"},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 202


async def test_tenants_do_not_share_an_auth_budget(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await create_two_tenants(client, platform_headers)
    limit = get_settings().auth_rate_limit_per_minute
    for _ in range(limit + 2):
        await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "Wrong-Pass-123456"},
            headers={"Host": HOST_A},
        )

    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "Wrong-Pass-123456"},
        headers={"Host": HOST_B},
    )
    assert resp.status_code == 401, "agency B inherited agency A's exhausted budget"


async def test_auth_limit_degrades_open_when_redis_is_down(
    client: AsyncClient, app: FastAPI, platform_headers: dict[str, str]
) -> None:
    await create_tenant(client, platform_headers)

    class BrokenRedis:
        def pipeline(self) -> Any:
            raise ConnectionError("redis is down")

    original = app.state.redis
    app.state.redis = BrokenRedis()
    try:
        for _ in range(get_settings().auth_rate_limit_per_minute + 5):
            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": "nobody@example.com", "password": "Wrong-Pass-123456"},
                headers={"Host": HOST_A},
            )
            assert resp.status_code == 401, "limiter failed closed with Redis down"
    finally:
        app.state.redis = original


async def test_global_limiter_degrades_open_when_redis_is_down(
    app: FastAPI, platform_headers: dict[str, str], client: AsyncClient
) -> None:
    await create_tenant(client, platform_headers)

    class BrokenRedis:
        def pipeline(self) -> Any:
            raise ConnectionError("redis is down")

    original = app.state.redis
    app.state.redis = BrokenRedis()
    try:
        resp = await client.get("/api/v1/site/config", headers={"Host": HOST_A})
        assert resp.status_code == 200
    finally:
        app.state.redis = original


async def test_global_limiter_429s_past_its_budget(platform_headers: dict[str, str]) -> None:
    """Built with a tiny budget rather than firing 300 real requests."""
    get_settings.cache_clear()
    settings = Settings(**{**get_settings().model_dump(), "global_rate_limit_per_minute": 3})
    application = create_app(settings)
    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            statuses = [
                (await c.get("/api/v1/site/config", headers={"Host": HOST_A})).status_code
                for _ in range(6)
            ]
        await application.state.redis.flushdb()
    assert 429 in statuses, f"global limiter never fired: {statuses}"
    get_settings.cache_clear()


async def test_health_probes_are_never_globally_limited(
    platform_headers: dict[str, str],
) -> None:
    """A busy load balancer must not end up rate-limiting its own health check."""
    get_settings.cache_clear()
    settings = Settings(**{**get_settings().model_dump(), "global_rate_limit_per_minute": 2})
    application = create_app(settings)
    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            statuses = [(await c.get("/healthz")).status_code for _ in range(8)]
        await application.state.redis.flushdb()
    assert set(statuses) == {200}
    get_settings.cache_clear()
