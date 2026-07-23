"""Tenant resolution middleware: Host → tenant, cache invalidation, site config."""

from httpx import AsyncClient

from tests.test_tenants_platform_api import create_tenant


async def test_site_config_for_resolved_tenant(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await create_tenant(client, platform_headers, settings={"brandColor": "#ff0000"})

    resp = await client.get("/api/v1/site/config", headers={"Host": "agency-a.test"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Agency A"
    assert body["slug"] == "agency-a"
    assert body["settings"] == {"brandColor": "#ff0000"}
    # Part 22 (§8.16): site config now also carries plan + usage + limits.
    assert body["plan"] == "trial"
    assert body["usage"] == {
        "listingsCount": 0,
        "agentsCount": 0,
        "storageBytes": 0,
        "emailsSent": 0,
    }
    assert body["limits"]["maxListings"] == 25


async def test_host_port_is_ignored(client: AsyncClient, platform_headers: dict[str, str]) -> None:
    await create_tenant(client, platform_headers)
    resp = await client.get("/api/v1/site/config", headers={"Host": "agency-a.test:8443"})
    assert resp.status_code == 200


async def test_unknown_domain_is_404_problem(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/site/config", headers={"Host": "nobody.test"})
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["type"].endswith("unknown-tenant")
    assert "request_id" in body


async def test_suspended_tenant_is_402_even_when_cached(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    created = await create_tenant(client, platform_headers)

    # Warm the domain cache, then suspend — invalidation must take effect.
    resp = await client.get("/api/v1/site/config", headers={"Host": "agency-a.test"})
    assert resp.status_code == 200

    resp = await client.post(
        f"/api/v1/platform/tenants/{created['id']}/suspend", headers=platform_headers
    )
    assert resp.status_code == 200

    resp = await client.get("/api/v1/site/config", headers={"Host": "agency-a.test"})
    assert resp.status_code == 402
    assert resp.json()["type"].endswith("tenant-suspended")


async def test_exempt_paths_need_no_tenant(client: AsyncClient) -> None:
    resp = await client.get("/healthz", headers={"Host": "nobody.test"})
    assert resp.status_code == 200
