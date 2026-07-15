"""Global error contract: everything leaving the API is RFC 9457 problem+json."""

from httpx import AsyncClient


async def test_unknown_route_returns_problem_json(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == 404
    assert body["instance"] == "/api/v1/does-not-exist"
    assert "request_id" in body


async def test_problem_body_has_no_internals(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/does-not-exist")
    text = resp.text.lower()
    assert "traceback" not in text
    assert "sqlalchemy" not in text
