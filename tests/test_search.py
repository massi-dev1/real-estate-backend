"""Search & discovery (§8.3): keyword FTS, attribute + geo filters, sorting
with the featured boost, map pins/clusters, sitemap and JSON-LD."""

import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient

from app.core.permissions import Role
from tests.helpers import HOST_A
from tests.test_listings import add_user, make_listing, tenant_and_login, transition

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

# ~360 km apart — comfortably outside every radius/bbox used below.
ALGIERS = {"lat": 36.7525, "lng": 3.042}
ORAN = {"lat": 35.698, "lng": -0.642}

PUBLIC = {"Host": HOST_A}


async def publish_listing(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    body = await make_listing(client, headers, **overrides)
    resp = await transition(client, headers, body["id"], "published")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def search(client: AsyncClient, **params: Any) -> dict[str, Any]:
    resp = await client.get("/api/v1/listings", params=params, headers=PUBLIC)
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def refs(page: dict[str, Any]) -> list[str]:
    return [item["referenceCode"] for item in page["items"]]


# ---- keyword search ----


async def test_keyword_search_with_french_stemming(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    flat = await publish_listing(
        client, admin, title={"fr": "Bel appartement lumineux au centre"}
    )
    await publish_listing(client, admin, title={"fr": "Villa avec piscine et jardin"})

    # Plural query stems to the singular title ("appartements" → "appart").
    page = await search(client, q="appartements")
    assert refs(page) == [flat["referenceCode"]]

    # Description text matches too (weight B).
    page = await search(client, q="lumineux")
    assert flat["referenceCode"] in refs(page)

    page = await search(client, q="hangar industriel")
    assert page["items"] == []


async def test_keyword_search_uses_requested_locale(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listed = await publish_listing(
        client,
        admin,
        title={"en": "Bright apartment near the sea", "fr": "Appartement lumineux"},
    )
    page = await search(client, q="apartments", locale="en")  # english stemming
    assert refs(page) == [listed["referenceCode"]]


# ---- attribute filters ----


async def test_features_filter_requires_all(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    both = await publish_listing(client, admin, features=["balcony", "pool"])
    await publish_listing(client, admin, features=["balcony"])

    page = await search(client, features=["balcony", "pool"])
    assert refs(page) == [both["referenceCode"]]

    resp = await client.get(
        "/api/v1/listings", params={"features": ["helipad"]}, headers=PUBLIC
    )
    assert resp.status_code == 422


async def test_area_min_and_city_filters(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    big = await publish_listing(
        client, admin, areaBuilt="120.00", address={"city": "Oran", "country": "DZ"}
    )
    await publish_listing(client, admin, areaBuilt="60.00")

    page = await search(client, areaMin="100")
    assert refs(page) == [big["referenceCode"]]

    page = await search(client, city="oran")  # case-insensitive
    assert refs(page) == [big["referenceCode"]]


# ---- geo filters ----


async def test_bbox_filter(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    algiers = await publish_listing(client, admin, location=ALGIERS)
    await publish_listing(client, admin, location=ORAN)

    page = await search(client, inBbox="2.8,36.5,3.3,37.0")  # around Algiers
    assert refs(page) == [algiers["referenceCode"]]

    resp = await client.get(
        "/api/v1/listings", params={"inBbox": "3.3,36.5,2.8,37.0"}, headers=PUBLIC
    )
    assert resp.status_code == 422  # min >= max


async def test_near_radius_filter(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    algiers = await publish_listing(client, admin, location=ALGIERS)
    await publish_listing(client, admin, location={"lat": 36.47, "lng": 2.829})  # Blida, ~40 km

    page = await search(client, near="3.05,36.75", radiusKm=10)
    assert refs(page) == [algiers["referenceCode"]]

    page = await search(client, near="3.05,36.75", radiusKm=100)  # reaches Blida
    assert len(page["items"]) == 2

    resp = await client.get("/api/v1/listings", params={"radiusKm": 5}, headers=PUBLIC)
    assert resp.status_code == 422  # radius without near


async def test_polygon_filter(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    algiers = await publish_listing(client, admin, location=ALGIERS)
    await publish_listing(client, admin, location=ORAN)

    triangle = "2.9 36.6,3.2 36.6,3.05 36.9"  # open ring — server closes it
    page = await search(client, inPolygon=triangle)
    assert refs(page) == [algiers["referenceCode"]]


async def test_geo_modes_are_mutually_exclusive(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.get(
        "/api/v1/listings",
        params={"inBbox": "2.8,36.5,3.3,37.0", "near": "3.05,36.75"},
        headers=PUBLIC,
    )
    assert resp.status_code == 422


# ---- sorting & featured ----


async def test_price_sort_with_cursor_pagination(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    for price in ("300.00", "100.00", "200.00"):
        await publish_listing(client, admin, price=price)

    page = await search(client, sort="price_asc", limit=2)
    prices = [Decimal(item["price"]) for item in page["items"]]
    assert prices == [Decimal("100.00"), Decimal("200.00")]
    assert page["nextCursor"]

    page2 = await search(client, sort="price_asc", limit=2, cursor=page["nextCursor"])
    assert [Decimal(item["price"]) for item in page2["items"]] == [Decimal("300.00")]
    assert page2["nextCursor"] is None

    # A cursor minted under one sort cannot page another.
    resp = await client.get(
        "/api/v1/listings",
        params={"sort": "price_desc", "cursor": page["nextCursor"]},
        headers=PUBLIC,
    )
    assert resp.status_code == 400


async def test_featured_leads_every_sort_and_is_manager_only(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    first = await publish_listing(client, admin)
    second = await publish_listing(client, admin)  # newest — would lead unsorted

    # Agents cannot self-feature (paid placement).
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="agent-f@a.example.com"
    )
    own = await make_listing(client, agent)
    resp = await client.patch(
        f"/api/v1/portal/listings/{own['id']}", json={"featured": True}, headers=agent
    )
    assert resp.status_code == 403

    resp = await client.patch(
        f"/api/v1/portal/listings/{first['id']}", json={"featured": True}, headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["featured"] is True

    page = await search(client)
    assert refs(page) == [first["referenceCode"], second["referenceCode"]]
    assert page["items"][0]["featured"] is True


# ---- map ----


async def test_map_pins(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    located = await publish_listing(client, admin, location=ALGIERS, price="150.00")
    await publish_listing(client, admin, location=None)  # never a pin

    resp = await client.get("/api/v1/listings/map", headers=PUBLIC)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["clustered"] is False
    assert body["clusters"] == []
    assert len(body["pins"]) == 1
    pin = body["pins"][0]
    assert pin["id"] == located["id"]
    assert pin["lat"] == pytest.approx(ALGIERS["lat"])
    assert pin["lng"] == pytest.approx(ALGIERS["lng"])
    assert Decimal(pin["price"]) == Decimal("150.00")
    assert pin["status"] == "published"


async def test_map_clusters_beyond_pin_limit(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await publish_listing(client, admin, location=ALGIERS)
    await publish_listing(client, admin, location={"lat": 36.76, "lng": 3.05})
    await publish_listing(client, admin, location=ORAN)

    monkeypatch.setattr("app.modules.listings.service.MAP_PIN_LIMIT", 2)
    # A country-scale viewport (span ≥ 5°) pins geohash precision to 3, whose
    # ~1.4° cells deterministically bucket the Algiers pair together and Oran
    # apart (cell boundaries fall at 2.8125° and 0° longitude).
    resp = await client.get(
        "/api/v1/listings/map", params={"inBbox": "-2,34,4,38"}, headers=PUBLIC
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["clustered"] is True
    assert body["pins"] == []
    assert sum(c["count"] for c in body["clusters"]) == 3
    assert len(body["clusters"]) == 2


# ---- SEO ----


async def test_sitemap_lists_published_only(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    published = await publish_listing(client, admin)
    draft = await make_listing(client, admin)

    resp = await client.get("/api/v1/sitemap.xml", headers=PUBLIC)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/xml")
    assert f"https://{HOST_A}/listings/{published['referenceCode']}" in resp.text
    assert draft["referenceCode"] not in resp.text
    assert "<lastmod>" in resp.text


async def test_detail_carries_json_ld(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listed = await publish_listing(client, admin)

    resp = await client.get(
        f"/api/v1/listings/{listed['referenceCode']}", headers=PUBLIC
    )
    assert resp.status_code == 200, resp.text
    json_ld = resp.json()["jsonLd"]
    assert json_ld["@type"] == "RealEstateListing"
    assert json_ld["@context"] == "https://schema.org"
    assert json_ld["identifier"] == listed["referenceCode"]
    assert Decimal(json_ld["offers"]["price"]) == Decimal("12500000.00")
    assert json_ld["offers"]["priceCurrency"] == "DZD"
    assert json_ld["geo"]["latitude"] == pytest.approx(ALGIERS["lat"])
    assert json_ld["address"]["addressLocality"] == "Alger"
    assert json_ld["numberOfRooms"] == 3

    # The list shape stays lean — no structured data per card.
    page = await search(client)
    assert page["items"][0]["jsonLd"] is None
