from httpx import AsyncClient


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
