"""Listings module (§8.1/§13): CRUD, reference codes, ownership scoping,
the publishing workflow, locale-negotiated public output, and tenant isolation."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from httpx import AsyncClient

from app.core.permissions import Role
from tests.conftest import FIXTURE_PASSWORD
from tests.helpers import HOST_A, HOST_B, bearer, login_user
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

LISTING_BODY: dict[str, Any] = {
    "purpose": "sale",
    "propertyType": "apartment",
    "title": {"fr": "Bel appartement F3", "ar": "شقة جميلة"},
    "description": {"fr": "Lumineux, proche du centre."},
    "price": "12500000.00",
    "beds": 3,
    "baths": 1,
    "areaBuilt": "85.50",
    "features": ["balcony", "elevator"],
    "address": {"city": "Alger", "country": "DZ"},
    "location": {"lat": 36.7525, "lng": 3.042},
}


async def tenant_and_login(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    role: Role,
    *,
    email: str | None = None,
    settings: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    tenant = await create_tenant(client, platform_headers, settings=settings)
    headers = await add_user(client, create_tenant_user, str(tenant["id"]), role, email=email)
    return tenant, headers


async def add_user(
    client: AsyncClient,
    create_tenant_user: CreateTenantUser,
    tenant_id: str,
    role: Role,
    *,
    email: str | None = None,
    host: str = HOST_A,
    existing: bool = False,
) -> dict[str, str]:
    """Login headers for a tenant user, creating the account first unless the
    caller already made it (``existing=True``, when it needs the user's id)."""
    email = email or f"{role.value}@a.example.com"
    if not existing:
        await create_tenant_user(tenant_id, email, role)
    resp = await login_user(client, host, email, FIXTURE_PASSWORD)
    assert resp.status_code == 200, resp.text
    return {"Host": host, "Authorization": bearer(resp)}


async def make_listing(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/portal/listings", json={**LISTING_BODY, **overrides}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def transition(
    client: AsyncClient, headers: dict[str, str], listing_id: str, to_status: str
) -> Any:
    return await client.post(
        f"/api/v1/portal/listings/{listing_id}/transition",
        json={"toStatus": to_status},
        headers=headers,
    )


# ---- create / read ----


async def test_create_listing(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    body = await make_listing(client, admin)

    assert body["status"] == "draft"
    assert body["referenceCode"].startswith("AGE-")  # slug "agency-a" → AGE
    assert body["referenceCode"].endswith("-00001")
    assert body["currency"] == "DZD"
    assert body["pricePeriod"] is None  # sale has no period
    assert body["title"] == {"fr": "Bel appartement F3", "ar": "شقة جميلة"}
    assert body["features"] == ["balcony", "elevator"]
    assert body["location"] == {"lat": 36.7525, "lng": 3.042}
    assert body["publishedAt"] is None

    # Reading it back (now WKB from the DB) round-trips the point.
    got = await client.get(f"/api/v1/portal/listings/{body['id']}", headers=admin)
    assert got.status_code == 200, got.text
    assert got.json()["location"] == {"lat": 36.7525, "lng": 3.042}


async def test_reference_codes_increment(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    first = await make_listing(client, admin)
    second = await make_listing(client, admin)
    assert first["referenceCode"].endswith("-00001")
    assert second["referenceCode"].endswith("-00002")


async def test_rent_purposes_get_price_period(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    monthly = await make_listing(client, admin, purpose="rent")
    daily = await make_listing(client, admin, purpose="rent_daily")
    assert monthly["pricePeriod"] == "month"
    assert daily["pricePeriod"] == "day"


async def test_create_validation_errors(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    for patch in (
        {"features": ["balcony", "moon_view"]},  # outside the vocabulary
        {"title": {"de": "Wohnung"}},  # unsupported locale key
        {"title": {"fr": "   "}},  # whitespace only = no content
        {"price": "-5"},
        {"location": {"lat": 95, "lng": 3}},
        {"floor": 5, "floorsTotal": 3},
        {"status": "published"},  # not an input field (extra=forbid)
    ):
        resp = await client.post(
            "/api/v1/portal/listings", json={**LISTING_BODY, **patch}, headers=admin
        )
        assert resp.status_code == 422, f"{patch}: {resp.status_code} {resp.text}"


# ---- update / delete ----


async def test_patch_updates_fields(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)
    resp = await client.patch(
        f"/api/v1/portal/listings/{listing['id']}",
        json={
            "price": "9990000.00",
            "title": {"fr": "Prix révisé"},
            "location": None,  # nullable → clearable
        },
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["price"] == "9990000.00"
    assert body["title"] == {"fr": "Prix révisé"}
    assert body["location"] is None
    assert body["beds"] == 3  # untouched fields survive


async def test_patch_rejects_null_for_required_and_purpose_change(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)
    url = f"/api/v1/portal/listings/{listing['id']}"
    assert (await client.patch(url, json={"title": None}, headers=admin)).status_code == 422
    assert (await client.patch(url, json={"purpose": "rent"}, headers=admin)).status_code == 422


async def test_delete_requires_archiving_published_first(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)
    url = f"/api/v1/portal/listings/{listing['id']}"
    assert (await transition(client, admin, listing["id"], "published")).status_code == 200

    assert (await client.delete(url, headers=admin)).status_code == 409

    assert (await transition(client, admin, listing["id"], "archived")).status_code == 200
    assert (await client.delete(url, headers=admin)).status_code == 204
    assert (await client.get(url, headers=admin)).status_code == 404
    listed = await client.get("/api/v1/portal/listings", headers=admin)
    assert listed.json()["items"] == []


async def test_duplicate_creates_fresh_draft(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)
    assert (await transition(client, admin, listing["id"], "published")).status_code == 200

    resp = await client.post(f"/api/v1/portal/listings/{listing['id']}/duplicate", headers=admin)
    assert resp.status_code == 201, resp.text
    copy = resp.json()
    assert copy["id"] != listing["id"]
    assert copy["referenceCode"] != listing["referenceCode"]
    assert copy["status"] == "draft"
    assert copy["publishedAt"] is None
    assert copy["title"] == listing["title"]
    assert copy["price"] == listing["price"]


# ---- workflow ----


async def test_workflow_and_history(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)

    assert (await transition(client, admin, listing["id"], "sold")).status_code == 409

    assert (await transition(client, admin, listing["id"], "review")).status_code == 200
    published = await transition(client, admin, listing["id"], "published")
    assert published.status_code == 200
    assert published.json()["publishedAt"] is not None
    assert (await transition(client, admin, listing["id"], "sold")).status_code == 200

    history = await client.get(f"/api/v1/portal/listings/{listing['id']}/history", headers=admin)
    assert history.status_code == 200
    moves = [(h["fromStatus"], h["toStatus"]) for h in history.json()]
    assert moves == [  # newest first
        ("published", "sold"),
        ("review", "published"),
        ("draft", "review"),
    ]


async def test_agent_publish_gated_by_tenant_setting(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, agent = await tenant_and_login(client, platform_headers, create_tenant_user, Role.AGENT)
    listing = await make_listing(client, agent)
    assert (await transition(client, agent, listing["id"], "published")).status_code == 403

    # Flip the tenant flag; the same agent can now self-publish.
    resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant['id']}",
        json={"settings": {"listings": {"agent_self_publish": True}}},
        headers=platform_headers,
    )
    assert resp.status_code == 200, resp.text
    assert (await transition(client, agent, listing["id"], "published")).status_code == 200


async def make_team_with(
    client: AsyncClient,
    admin: dict[str, str],
    lead_user_id: str,
    member_ids: list[str],
) -> dict[str, Any]:
    """Admin creates a team and adds members — §8.5 membership is what gives a
    team_lead visibility over the members' listings and leads."""
    resp = await client.post(
        "/api/v1/portal/teams",
        json={"name": "Team", "leadUserId": lead_user_id},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    team = dict(resp.json())
    for member_id in member_ids:
        added = await client.post(
            f"/api/v1/portal/teams/{team['id']}/members",
            json={"userId": member_id},
            headers=admin,
        )
        assert added.status_code == 201, added.text
    return team


async def test_team_lead_publishes_for_their_team_agents(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    tid = str(tenant["id"])
    agent_id = await create_tenant_user(tid, "agent@a.example.com", Role.AGENT)
    lead_id = await create_tenant_user(tid, "lead@a.example.com", Role.TEAM_LEAD)
    agent = await add_user(
        client, create_tenant_user, tid, Role.AGENT, email="agent@a.example.com", existing=True
    )
    lead = await add_user(
        client, create_tenant_user, tid, Role.TEAM_LEAD, email="lead@a.example.com", existing=True
    )
    listing = await make_listing(client, agent)

    # Since §8.5 a team lead only reaches listings inside their team: without
    # membership the transition is a scoped 404, with it a 200.
    assert (await transition(client, lead, listing["id"], "published")).status_code == 404
    await make_team_with(client, admin, str(lead_id), [str(agent_id)])
    assert (await transition(client, lead, listing["id"], "published")).status_code == 200


# ---- ownership scoping (§7.2) ----


async def test_agents_are_scoped_to_their_own_listings(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, agent_one = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.AGENT, email="agent1@a.example.com"
    )
    agent_two = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="agent2@a.example.com"
    )
    admin = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.ADMIN, email="admin@a.example.com"
    )
    # A team lead with no team sees only their own rows since §8.5.
    lead = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.TEAM_LEAD, email="lead@a.example.com"
    )
    mine = await make_listing(client, agent_one)
    url = f"/api/v1/portal/listings/{mine['id']}"

    # The other agent gets 404 (not 403 — no existence oracle) on every verb.
    assert (await client.get(url, headers=agent_two)).status_code == 404
    patched = await client.patch(url, json={"price": "1.00"}, headers=agent_two)
    assert patched.status_code == 404
    assert (await client.delete(url, headers=agent_two)).status_code == 404
    assert (await transition(client, agent_two, mine["id"], "review")).status_code == 404

    # Lists: owner and admin see it; the other agent and the teamless lead don't.
    for headers, expected in ((agent_one, 1), (agent_two, 0), (lead, 0), (admin, 1)):
        listed = await client.get("/api/v1/portal/listings", headers=headers)
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == expected
        assert listed.json()["totalEstimate"] == expected


async def test_agent_cannot_assign_someone_else(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, agent = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.AGENT, email="agent1@a.example.com"
    )
    admin = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.ADMIN, email="admin@a.example.com"
    )
    other = await create_tenant_user(str(tenant["id"]), "agent2@a.example.com", Role.AGENT)

    resp = await client.post(
        "/api/v1/portal/listings",
        json={**LISTING_BODY, "agentId": str(other)},
        headers=agent,
    )
    assert resp.status_code == 403

    # An admin may assign any active tenant account — but not a ghost.
    assigned = await make_listing(client, admin, agentId=str(other))
    assert assigned["agentId"] == str(other)
    ghost = await client.post(
        "/api/v1/portal/listings",
        json={**LISTING_BODY, "agentId": str(uuid.uuid4())},
        headers=admin,
    )
    assert ghost.status_code == 409


async def test_unassign_agent_is_manager_only(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, agent = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.AGENT, email="agent1@a.example.com"
    )
    admin = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.ADMIN, email="admin@a.example.com"
    )
    listing = await make_listing(client, agent)
    url = f"/api/v1/portal/listings/{listing['id']}"

    denied = await client.patch(url, json={"agentId": None}, headers=agent)
    assert denied.status_code == 403

    cleared = await client.patch(url, json={"agentId": None}, headers=admin)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["agentId"] is None  # unassigned — not silently reassigned


async def test_rbac_and_auth_guards(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, buyer = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.BUYER_RENTER
    )
    resp = await client.post("/api/v1/portal/listings", json=LISTING_BODY, headers=buyer)
    assert resp.status_code == 403
    resp = await client.get("/api/v1/portal/listings", headers={"Host": HOST_A})
    assert resp.status_code == 401


# ---- tenant isolation ----


async def test_listings_never_leak_across_tenants(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin_a = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    tenant_b = await create_tenant(
        client, platform_headers, name="Agency B", slug="agency-b", domain=HOST_B
    )
    admin_b = await add_user(
        client,
        create_tenant_user,
        str(tenant_b["id"]),
        Role.ADMIN,
        email="admin@b.example.com",
        host=HOST_B,
    )
    listing_b = await make_listing(client, admin_b)
    assert (await transition(client, admin_b, listing_b["id"], "published")).status_code == 200

    # Portal: tenant A's admin can't address tenant B's listing.
    url = f"/api/v1/portal/listings/{listing_b['id']}"
    assert (await client.get(url, headers=admin_a)).status_code == 404
    assert (await client.get("/api/v1/portal/listings", headers=admin_a)).json()["items"] == []

    # Public: B's published listing is invisible on A's site, by list and ref.
    on_a = await client.get("/api/v1/listings", headers={"Host": HOST_A})
    assert on_a.json()["items"] == []
    by_ref = await client.get(
        f"/api/v1/listings/{listing_b['referenceCode']}", headers={"Host": HOST_A}
    )
    assert by_ref.status_code == 404


# ---- public site ----


async def test_public_shows_only_published(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    draft = await make_listing(client, admin)
    live = await make_listing(client, admin)
    assert (await transition(client, admin, live["id"], "published")).status_code == 200

    resp = await client.get("/api/v1/listings", headers={"Host": HOST_A})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [x["id"] for x in items] == [live["id"]]
    # Public shape: negotiated strings, no workflow internals.
    assert items[0]["title"] == "Bel appartement F3"
    assert "status" not in items[0]
    assert "agentId" not in items[0]

    # Detail works by reference code and by id — but never for drafts.
    by_ref = await client.get(f"/api/v1/listings/{live['referenceCode']}", headers={"Host": HOST_A})
    assert by_ref.status_code == 200
    assert by_ref.json()["id"] == live["id"]
    by_id = await client.get(f"/api/v1/listings/{live['id']}", headers={"Host": HOST_A})
    assert by_id.status_code == 200
    hidden = await client.get(f"/api/v1/listings/{draft['id']}", headers={"Host": HOST_A})
    assert hidden.status_code == 404


async def test_public_locale_negotiation(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)
    assert (await transition(client, admin, listing["id"], "published")).status_code == 200
    url = f"/api/v1/listings/{listing['referenceCode']}"

    arabic = await client.get(url, params={"locale": "ar"}, headers={"Host": HOST_A})
    assert arabic.json()["locale"] == "ar"
    assert arabic.json()["title"] == "شقة جميلة"

    negotiated = await client.get(
        url, headers={"Host": HOST_A, "Accept-Language": "ar-DZ,ar;q=0.9,fr;q=0.8"}
    )
    assert negotiated.json()["title"] == "شقة جميلة"

    # Requested locale has no translation → fallback chain fills the hole.
    english = await client.get(url, params={"locale": "en"}, headers={"Host": HOST_A})
    assert english.json()["locale"] == "en"
    assert english.json()["title"] == "Bel appartement F3"
    assert (await client.get(url, headers={"Host": HOST_A})).json()["locale"] == "fr"


async def test_public_filters(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    sale = await make_listing(client, admin, price="12500000.00")
    rent = await make_listing(client, admin, purpose="rent", price="80000.00", beds=1)
    for listing in (sale, rent):
        assert (await transition(client, admin, listing["id"], "published")).status_code == 200

    async def ids(**params: Any) -> list[str]:
        resp = await client.get("/api/v1/listings", params=params, headers={"Host": HOST_A})
        assert resp.status_code == 200, resp.text
        return [x["id"] for x in resp.json()["items"]]

    assert set(await ids()) == {sale["id"], rent["id"]}
    assert await ids(purpose="rent") == [rent["id"]]
    assert await ids(priceMax="100000") == [rent["id"]]
    assert await ids(priceMin="1000000") == [sale["id"]]
    assert await ids(bedsMin=2) == [sale["id"]]
    resp = await client.get(
        "/api/v1/listings",
        params={"priceMin": "10", "priceMax": "5"},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 422


async def test_pagination_cursors(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    created = [await make_listing(client, admin) for _ in range(3)]
    for listing in created:
        assert (await transition(client, admin, listing["id"], "published")).status_code == 200

    # Public: newest published first, keyset continues without overlap.
    first = await client.get("/api/v1/listings", params={"limit": 2}, headers={"Host": HOST_A})
    page_one = first.json()
    assert len(page_one["items"]) == 2
    assert page_one["nextCursor"]
    second = await client.get(
        "/api/v1/listings",
        params={"limit": 2, "cursor": page_one["nextCursor"]},
        headers={"Host": HOST_A},
    )
    page_two = second.json()
    assert len(page_two["items"]) == 1
    assert page_two["nextCursor"] is None
    seen = [x["id"] for x in page_one["items"] + page_two["items"]]
    assert sorted(seen) == sorted(x["id"] for x in created)

    # Portal: same mechanics plus a total, and garbage cursors are a 400.
    portal = await client.get("/api/v1/portal/listings", params={"limit": 2}, headers=admin)
    assert portal.json()["totalEstimate"] == 3
    assert portal.json()["nextCursor"]
    bad = await client.get(
        "/api/v1/portal/listings", params={"cursor": "not-a-cursor"}, headers=admin
    )
    assert bad.status_code == 400

    # Status filter narrows portal lists.
    drafts = await client.get("/api/v1/portal/listings", params={"status": "draft"}, headers=admin)
    assert drafts.json()["items"] == []
