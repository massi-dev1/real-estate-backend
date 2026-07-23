from httpx import AsyncClient

from tests.test_tenants_platform_api import create_tenant


async def test_healthz(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_reports_dependencies(client: AsyncClient) -> None:
    resp = await client.get("/readyz")
    assert resp.status_code == 200, f"stack not ready: {resp.json()}"
    body = resp.json()
    assert body == {"status": "ok", "database": "up", "redis": "up"}


async def test_request_id_header_echoed(client: AsyncClient) -> None:
    resp = await client.get("/healthz", headers={"X-Request-ID": "req-abc-123"})
    assert resp.headers["x-request-id"] == "req-abc-123"


async def test_security_headers_present(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"


async def test_tls_check_approves_known_tenant_domain(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    # Caddy's on-demand-TLS ask handler (§16): a verified tenant domain → 200.
    await create_tenant(client, platform_headers)
    resp = await client.get("/internal/tls-check", params={"domain": "agency-a.test"})
    assert resp.status_code == 200


async def test_tls_check_rejects_unknown_domain(client: AsyncClient) -> None:
    resp = await client.get("/internal/tls-check", params={"domain": "attacker.example"})
    assert resp.status_code == 404


async def test_tls_check_is_tenant_exempt(client: AsyncClient) -> None:
    # The endpoint is called with no tenant Host and must not 404 on tenant
    # resolution — an unknown domain returns the endpoint's own 404, and a
    # missing domain param is a 422 (validation), never a tenant-middleware 404.
    resp = await client.get("/internal/tls-check")
    assert resp.status_code == 422
