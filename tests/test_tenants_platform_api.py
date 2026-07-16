"""Platform tenant CRUD: creation, domains, status transitions, pagination."""

from typing import Any

from httpx import AsyncClient


async def create_tenant(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    name: str = "Agency A",
    slug: str = "agency-a",
    domain: str = "agency-a.test",
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/platform/tenants",
        json={"name": name, "slug": slug, "domain": domain, "settings": settings or {}},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_create_tenant(client: AsyncClient, platform_headers: dict[str, str]) -> None:
    body = await create_tenant(client, platform_headers, settings={"brand_color": "#123456"})
    assert body["name"] == "Agency A"
    assert body["slug"] == "agency-a"
    assert body["status"] == "trial"
    assert body["settings"] == {"brand_color": "#123456"}
    # camelCase on the wire
    assert "createdAt" in body
    assert len(body["domains"]) == 1
    assert body["domains"][0]["domain"] == "agency-a.test"
    assert body["domains"][0]["isPrimary"] is True


async def test_platform_auth_required(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/platform/tenants")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")

    resp = await client.get(
        "/api/v1/platform/tenants", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert resp.status_code == 401


async def test_duplicate_slug_and_domain_conflict(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await create_tenant(client, platform_headers)
    resp = await client.post(
        "/api/v1/platform/tenants",
        json={"name": "Other", "slug": "agency-a", "domain": "other.test"},
        headers=platform_headers,
    )
    assert resp.status_code == 409

    resp = await client.post(
        "/api/v1/platform/tenants",
        json={"name": "Other", "slug": "agency-b", "domain": "agency-a.test"},
        headers=platform_headers,
    )
    assert resp.status_code == 409


async def test_invalid_payload_rejected(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    resp = await client.post(
        "/api/v1/platform/tenants",
        json={"name": "X", "slug": "Bad Slug!", "domain": "not a domain"},
        headers=platform_headers,
    )
    assert resp.status_code == 422

    resp = await client.post(
        "/api/v1/platform/tenants",
        json={"name": "X", "slug": "ok-slug", "domain": "ok.test", "unknown": 1},
        headers=platform_headers,
    )
    assert resp.status_code == 422


async def test_get_and_update_tenant(client: AsyncClient, platform_headers: dict[str, str]) -> None:
    created = await create_tenant(client, platform_headers)

    resp = await client.get(f"/api/v1/platform/tenants/{created['id']}", headers=platform_headers)
    assert resp.status_code == 200
    assert resp.json()["slug"] == "agency-a"

    resp = await client.patch(
        f"/api/v1/platform/tenants/{created['id']}",
        json={"name": "Renamed", "settings": {"locale": "fr"}},
        headers=platform_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["settings"] == {"locale": "fr"}

    resp = await client.get(
        "/api/v1/platform/tenants/00000000-0000-0000-0000-000000000000",
        headers=platform_headers,
    )
    assert resp.status_code == 404


async def test_suspend_and_activate(client: AsyncClient, platform_headers: dict[str, str]) -> None:
    created = await create_tenant(client, platform_headers)

    resp = await client.post(
        f"/api/v1/platform/tenants/{created['id']}/suspend", headers=platform_headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "suspended"

    resp = await client.post(
        f"/api/v1/platform/tenants/{created['id']}/activate", headers=platform_headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


async def test_domain_management(client: AsyncClient, platform_headers: dict[str, str]) -> None:
    created = await create_tenant(client, platform_headers)
    tenant_id = created["id"]

    resp = await client.post(
        f"/api/v1/platform/tenants/{tenant_id}/domains",
        json={"domain": "www.agency-a.test", "isPrimary": True},
        headers=platform_headers,
    )
    assert resp.status_code == 201
    domains = {d["domain"]: d for d in resp.json()["domains"]}
    assert domains["www.agency-a.test"]["isPrimary"] is True
    assert domains["agency-a.test"]["isPrimary"] is False

    # The (new) primary domain cannot be removed.
    resp = await client.delete(
        f"/api/v1/platform/tenants/{tenant_id}/domains/{domains['www.agency-a.test']['id']}",
        headers=platform_headers,
    )
    assert resp.status_code == 409

    resp = await client.delete(
        f"/api/v1/platform/tenants/{tenant_id}/domains/{domains['agency-a.test']['id']}",
        headers=platform_headers,
    )
    assert resp.status_code == 200
    assert [d["domain"] for d in resp.json()["domains"]] == ["www.agency-a.test"]


async def test_list_pagination(client: AsyncClient, platform_headers: dict[str, str]) -> None:
    for i in range(3):
        await create_tenant(
            client,
            platform_headers,
            name=f"Agency {i}",
            slug=f"agency-{i}",
            domain=f"agency-{i}.test",
        )

    resp = await client.get("/api/v1/platform/tenants?limit=2", headers=platform_headers)
    assert resp.status_code == 200
    page = resp.json()
    assert len(page["items"]) == 2
    assert page["totalEstimate"] == 3
    assert page["nextCursor"]
    # Newest first (uuid7 / created_at keyset).
    assert page["items"][0]["slug"] == "agency-2"

    resp = await client.get(
        f"/api/v1/platform/tenants?limit=2&cursor={page['nextCursor']}",
        headers=platform_headers,
    )
    page2 = resp.json()
    assert [t["slug"] for t in page2["items"]] == ["agency-0"]
    assert page2["nextCursor"] is None

    resp = await client.get(
        "/api/v1/platform/tenants?cursor=not-a-cursor", headers=platform_headers
    )
    assert resp.status_code == 400
