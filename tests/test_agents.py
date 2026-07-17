"""Agents & teams module (§8.5/§13): profile CRUD + ownership, curated
public directory, photo pipeline, teams & membership, territory-based lead
assignment, team-scoped team_lead visibility, and the stats slice."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from httpx import AsyncClient

from app.core.permissions import Role
from tests.conftest import FIXTURE_PASSWORD
from tests.helpers import HOST_A, HOST_B, bearer, login_user
from tests.test_leads import capture, capture_body
from tests.test_listings import add_user, make_listing, tenant_and_login, transition
from tests.test_media import jpeg_bytes
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

PORTAL_AGENTS = "/api/v1/portal/agents"
PORTAL_TEAMS = "/api/v1/portal/teams"
PUBLIC_AGENTS = "/api/v1/agents"

# Ring around Algiers (LISTING_BODY's location sits inside it).
ALGIERS_RING = [[2.9, 36.6], [3.2, 36.6], [3.2, 36.9], [2.9, 36.9]]
# Oran — far outside the ring.
ORAN_POINT = {"lat": 35.6971, "lng": -0.6308}

PROFILE_BODY: dict[str, Any] = {
    "slug": "sam-the-agent",
    "bio": {"fr": "Agent immobilier à Alger.", "en": "Real-estate agent in Algiers."},
    "specialties": ["residential_sales", "luxury"],
    "serviceAreas": [ALGIERS_RING],
    "licenseNo": "DZ-12345",
    "socials": {"instagram": "https://instagram.com/sam"},
}


async def login_headers(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await login_user(client, HOST_A, email, FIXTURE_PASSWORD)
    assert resp.status_code == 200, resp.text
    return {"Host": HOST_A, "Authorization": bearer(resp)}


async def make_profile(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    resp = await client.post(
        PORTAL_AGENTS, json={**PROFILE_BODY, **overrides}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def publish_profile(
    client: AsyncClient, admin: dict[str, str], profile_id: str
) -> None:
    resp = await client.patch(
        f"{PORTAL_AGENTS}/{profile_id}", json={"isPublished": True}, headers=admin
    )
    assert resp.status_code == 200, resp.text


async def make_team(
    client: AsyncClient,
    admin: dict[str, str],
    *,
    lead_user_id: str | None = None,
    member_ids: list[str] | None = None,
    name: str = "Alpha",
) -> dict[str, Any]:
    resp = await client.post(
        PORTAL_TEAMS, json={"name": name, "leadUserId": lead_user_id}, headers=admin
    )
    assert resp.status_code == 201, resp.text
    team = dict(resp.json())
    for member_id in member_ids or []:
        added = await client.post(
            f"{PORTAL_TEAMS}/{team['id']}/members", json={"userId": member_id}, headers=admin
        )
        assert added.status_code == 201, added.text
    return team


# ---- profile CRUD & ownership ----


async def test_agent_creates_own_profile_and_roundtrips(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, agent = await tenant_and_login(client, platform_headers, create_tenant_user, Role.AGENT)
    body = await make_profile(client, agent)

    assert body["slug"] == "sam-the-agent"
    assert body["bio"]["fr"] == "Agent immobilier à Alger."
    assert body["specialties"] == ["luxury", "residential_sales"]
    assert body["isPublished"] is False
    assert body["photoStatus"] is None
    # The open input ring comes back closed (first point appended last).
    assert body["serviceAreas"] == [[*ALGIERS_RING, ALGIERS_RING[0]]]

    # Reading it back (now WKB from the DB) round-trips the rings.
    own = await client.get(f"{PORTAL_AGENTS}/me", headers=agent)
    assert own.status_code == 200, own.text
    assert own.json()["serviceAreas"] == [[*ALGIERS_RING, ALGIERS_RING[0]]]


async def test_profile_ownership_and_permission_gates(
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
    profile = await make_profile(client, agent_one)
    url = f"{PORTAL_AGENTS}/{profile['id']}"

    # The other agent gets 404 (no existence oracle) on read and write.
    assert (await client.get(url, headers=agent_two)).status_code == 404
    patched = await client.patch(url, json={"licenseNo": "X"}, headers=agent_two)
    assert patched.status_code == 404

    # The owner edits their bio but cannot self-publish into the directory.
    ok = await client.patch(url, json={"bio": {"fr": "Mise à jour."}}, headers=agent_one)
    assert ok.status_code == 200 and ok.json()["bio"] == {"fr": "Mise à jour."}
    denied = await client.patch(url, json={"isPublished": True}, headers=agent_one)
    assert denied.status_code == 403
    await publish_profile(client, admin, profile["id"])

    # Roster list is manager-only; /me works for the owner.
    assert (await client.get(PORTAL_AGENTS, headers=agent_one)).status_code == 403
    roster = await client.get(PORTAL_AGENTS, headers=admin)
    assert roster.status_code == 200 and len(roster.json()) == 1

    # A buyer can't have a profile; a duplicate slug/user conflicts.
    buyer = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.BUYER_RENTER,
        email="buyer@a.example.com",
    )
    assert (
        await client.post(PORTAL_AGENTS, json=PROFILE_BODY, headers=buyer)
    ).status_code == 409
    dup_user = await client.post(
        PORTAL_AGENTS, json={**PROFILE_BODY, "slug": "other-slug"}, headers=agent_one
    )
    assert dup_user.status_code == 409
    dup_slug = await client.post(PORTAL_AGENTS, json=PROFILE_BODY, headers=agent_two)
    assert dup_slug.status_code == 409


async def test_manager_creates_profile_for_agent(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    agent_id = await create_tenant_user(str(tenant["id"]), "agent@a.example.com", Role.AGENT)
    body = await make_profile(client, admin, userId=str(agent_id))
    assert body["userId"] == str(agent_id)

    # An agent cannot create a profile for someone else.
    agent = await login_headers(client, "agent@a.example.com")
    other_id = await create_tenant_user(str(tenant["id"]), "agent2@a.example.com", Role.AGENT)
    denied = await client.post(
        PORTAL_AGENTS,
        json={**PROFILE_BODY, "slug": "not-mine", "userId": str(other_id)},
        headers=agent,
    )
    assert denied.status_code == 403


# ---- public directory ----


async def test_public_directory_and_detail(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, agent = await tenant_and_login(client, platform_headers, create_tenant_user, Role.AGENT)
    admin = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.ADMIN, email="admin@a.example.com"
    )
    profile = await make_profile(client, agent)

    # Unpublished profiles are invisible.
    empty = await client.get(PUBLIC_AGENTS, headers={"Host": HOST_A})
    assert empty.status_code == 200 and empty.json()["items"] == []
    assert (
        await client.get(f"{PUBLIC_AGENTS}/sam-the-agent", headers={"Host": HOST_A})
    ).status_code == 404

    await publish_profile(client, admin, profile["id"])

    # A published listing assigned to the agent appears on the profile page.
    listing = await make_listing(client, agent)
    assert (await transition(client, admin, listing["id"], "published")).status_code == 200

    cards = await client.get(PUBLIC_AGENTS, headers={"Host": HOST_A})
    assert cards.status_code == 200, cards.text
    items = cards.json()["items"]
    assert len(items) == 1
    card = items[0]
    assert card["displayName"] == "agent"  # email local part fallback
    assert card["locale"] == "fr" and card["bio"] == PROFILE_BODY["bio"]["fr"]
    assert "serviceAreas" not in card  # territory maps are back-office data

    detail = await client.get(
        f"{PUBLIC_AGENTS}/sam-the-agent", params={"locale": "en"}, headers={"Host": HOST_A}
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["bio"] == PROFILE_BODY["bio"]["en"]
    assert [x["id"] for x in body["listings"]] == [listing["id"]]

    # Specialty filter is a controlled vocabulary.
    filtered = await client.get(
        PUBLIC_AGENTS, params={"specialty": "commercial"}, headers={"Host": HOST_A}
    )
    assert filtered.status_code == 200 and filtered.json()["items"] == []
    assert (
        await client.get(PUBLIC_AGENTS, params={"specialty": "nope"}, headers={"Host": HOST_A})
    ).status_code == 422

    # Tenant isolation: agency B's site has no agents.
    other = await create_tenant(
        client, platform_headers, name="Agency B", slug="agency-b", domain=HOST_B
    )
    assert other
    cross = await client.get(PUBLIC_AGENTS, headers={"Host": HOST_B})
    assert cross.status_code == 200 and cross.json()["items"] == []


# ---- photo pipeline ----


async def test_photo_upload_and_processing(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, agent = await tenant_and_login(client, platform_headers, create_tenant_user, Role.AGENT)
    profile = await make_profile(client, agent)
    url = f"{PORTAL_AGENTS}/{profile['id']}"

    data = jpeg_bytes(800, 800)
    resp = await client.post(
        f"{url}/photo/uploads",
        json={"contentType": "image/jpeg", "sizeBytes": len(data)},
        headers=agent,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["profile"]["photoStatus"] == "pending"

    async with httpx.AsyncClient() as direct:
        put = await direct.put(body["uploadUrl"], content=data, headers=body["uploadHeaders"])
        assert put.status_code == 200, put.text

    confirm = await client.post(f"{url}/photo/confirm", headers=agent)
    assert confirm.status_code == 202, confirm.text

    # Eager Celery processed inline during the post-commit hook.
    final = await client.get(url, headers=agent)
    assert final.status_code == 200
    photo = final.json()
    assert photo["photoStatus"] == "ready", photo
    assert set(photo["photoVariants"]) == {
        f"{name}_{fmt}" for name in ("avatar", "card") for fmt in ("webp", "jpeg")
    }
    avatar = photo["photoVariants"]["avatar_webp"]
    assert avatar["width"] == 320
    async with httpx.AsyncClient() as direct:
        got = await direct.get(avatar["url"])
        assert got.status_code == 200
        assert got.headers["content-type"] == "image/webp"

    # Garbage bytes fail validation permanently.
    resp = await client.post(
        f"{url}/photo/uploads",
        json={"contentType": "image/png", "sizeBytes": 12},
        headers=agent,
    )
    assert resp.status_code == 201
    body = resp.json()
    async with httpx.AsyncClient() as direct:
        put = await direct.put(
            body["uploadUrl"], content=b"not-an-image", headers=body["uploadHeaders"]
        )
        assert put.status_code == 200
    assert (await client.post(f"{url}/photo/confirm", headers=agent)).status_code == 202
    failed = (await client.get(url, headers=agent)).json()
    assert failed["photoStatus"] == "failed"
    assert failed["photoError"]


# ---- teams & membership ----


async def test_team_crud_and_membership_rules(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    tid = str(tenant["id"])
    tl_one = await create_tenant_user(tid, "lead1@a.example.com", Role.TEAM_LEAD)
    tl_two = await create_tenant_user(tid, "lead2@a.example.com", Role.TEAM_LEAD)
    agent_id = await create_tenant_user(tid, "agent@a.example.com", Role.AGENT)
    buyer_id = await create_tenant_user(tid, "buyer@a.example.com", Role.BUYER_RENTER)

    team = await make_team(client, admin, lead_user_id=str(tl_one), member_ids=[str(agent_id)])
    team_url = f"{PORTAL_TEAMS}/{team['id']}"

    # The team's lead manages membership; a foreign lead gets 404; an agent 403.
    lead_one = await login_headers(client, "lead1@a.example.com")
    lead_two = await login_headers(client, "lead2@a.example.com")
    agent = await login_headers(client, "agent@a.example.com")

    added = await client.post(
        f"{team_url}/members", json={"userId": str(tl_two)}, headers=lead_one
    )
    assert added.status_code == 201, added.text
    assert (
        await client.post(f"{team_url}/members", json={"userId": str(tl_two)}, headers=lead_two)
    ).status_code == 404
    assert (
        await client.get(f"{team_url}/members", headers=agent)
    ).status_code == 403  # no AGENT_MANAGE

    # Members must be active agent/team-lead accounts; duplicates conflict.
    assert (
        await client.post(f"{team_url}/members", json={"userId": str(buyer_id)}, headers=admin)
    ).status_code == 409
    assert (
        await client.post(f"{team_url}/members", json={"userId": str(agent_id)}, headers=admin)
    ).status_code == 409

    detail = await client.get(team_url, headers=lead_one)
    assert detail.status_code == 200
    assert {m["userId"] for m in detail.json()["members"]} == {str(agent_id), str(tl_two)}

    removed = await client.delete(f"{team_url}/members/{tl_two}", headers=lead_one)
    assert removed.status_code == 204

    # Team create/delete and lead reassignment are admin-only.
    assert (
        await client.post(PORTAL_TEAMS, json={"name": "Beta"}, headers=lead_one)
    ).status_code == 403
    assert (
        await client.patch(team_url, json={"leadUserId": str(tl_two)}, headers=lead_one)
    ).status_code == 403
    renamed = await client.patch(team_url, json={"name": "Alpha+"}, headers=lead_one)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Alpha+"
    assert (await client.delete(team_url, headers=lead_one)).status_code == 403
    assert (await client.delete(team_url, headers=admin)).status_code == 204


# ---- team-scoped visibility (§8.5, deferred from Parts 4/8) ----


async def test_team_lead_sees_exactly_their_team(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    tid = str(tenant["id"])
    tl_id = await create_tenant_user(tid, "lead@a.example.com", Role.TEAM_LEAD)
    member_id = await create_tenant_user(tid, "member@a.example.com", Role.AGENT)
    outsider_id = await create_tenant_user(tid, "outsider@a.example.com", Role.AGENT)
    await make_team(client, admin, lead_user_id=str(tl_id), member_ids=[str(member_id)])

    lead = await login_headers(client, "lead@a.example.com")
    member = await login_headers(client, "member@a.example.com")
    outsider = await login_headers(client, "outsider@a.example.com")

    own_listing = await make_listing(client, lead)
    member_listing = await make_listing(client, member)
    outsider_listing = await make_listing(client, outsider)

    # Listings: own + member's, not the outsider's; scoped miss is 404.
    listed = await client.get("/api/v1/portal/listings", headers=lead)
    assert listed.status_code == 200
    assert {x["id"] for x in listed.json()["items"]} == {
        own_listing["id"],
        member_listing["id"],
    }
    assert (
        await client.get(f"/api/v1/portal/listings/{member_listing['id']}", headers=lead)
    ).status_code == 200
    assert (
        await client.get(f"/api/v1/portal/listings/{outsider_listing['id']}", headers=lead)
    ).status_code == 404

    # Leads: same shape. Admin logs two manual leads for member and outsider.
    for agent_uuid in (member_id, outsider_id):
        resp = await client.post(
            "/api/v1/portal/leads",
            json={
                "contact": {"email": f"c-{agent_uuid}@example.com"},
                "source": "phone",
                "agentId": str(agent_uuid),
            },
            headers=admin,
        )
        assert resp.status_code == 201, resp.text

    lead_items = await client.get("/api/v1/portal/leads", headers=lead)
    assert lead_items.status_code == 200
    assert {x["agentId"] for x in lead_items.json()["items"]} == {str(member_id)}

    # Admin still sees the whole tenant.
    all_items = await client.get("/api/v1/portal/leads", headers=admin)
    assert len(all_items.json()["items"]) == 2


# ---- territory assignment (§8.4, deferred from Part 8) ----


async def setup_territory(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="terr@a.example.com"
    )
    return tenant, admin, agent


async def test_territory_rule_requires_published_service_areas(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, agent = await setup_territory(client, platform_headers, create_tenant_user)
    denied = await client.put(
        "/api/v1/portal/leads/assignment-rule",
        json={"strategy": "territory", "config": {}},
        headers=admin,
    )
    assert denied.status_code == 409  # no published profile with areas yet

    profile = await make_profile(client, agent)
    denied = await client.put(
        "/api/v1/portal/leads/assignment-rule",
        json={"strategy": "territory", "config": {}},
        headers=admin,
    )
    assert denied.status_code == 409  # profile exists but is not published

    await publish_profile(client, admin, profile["id"])
    ok = await client.put(
        "/api/v1/portal/leads/assignment-rule",
        json={"strategy": "territory", "config": {}},
        headers=admin,
    )
    assert ok.status_code == 200, ok.text


async def test_territory_assignment_matches_listing_point(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, agent = await setup_territory(client, platform_headers, create_tenant_user)
    profile = await make_profile(client, agent)
    await publish_profile(client, admin, profile["id"])
    rule = await client.put(
        "/api/v1/portal/leads/assignment-rule",
        json={"strategy": "territory", "config": {}},
        headers=admin,
    )
    assert rule.status_code == 200

    # A listing inside the ring (Algiers) routes its lead to the agent.
    inside = await make_listing(client, admin, agentId=None)
    assert (await transition(client, admin, inside["id"], "published")).status_code == 200
    captured = await capture(
        client, capture_body(email="in@example.com", listingId=inside["id"])
    )
    assert captured.status_code == 201, captured.text
    got = await client.get(f"/api/v1/portal/leads/{captured.json()['id']}", headers=admin)
    assert got.status_code == 200
    agent_email_row = await client.get("/api/v1/users/me", headers=agent)
    assert got.json()["agentId"] == agent_email_row.json()["id"]

    # A listing outside every service area stays unassigned.
    outside = await make_listing(client, admin, agentId=None, location=ORAN_POINT)
    assert (await transition(client, admin, outside["id"], "published")).status_code == 200
    captured = await capture(
        client, capture_body(email="out@example.com", listingId=outside["id"])
    )
    assert captured.status_code == 201
    got = await client.get(f"/api/v1/portal/leads/{captured.json()['id']}", headers=admin)
    assert got.json()["agentId"] is None

    # No listing attached → unassigned too.
    captured = await capture(client, capture_body(email="none@example.com"))
    got = await client.get(f"/api/v1/portal/leads/{captured.json()['id']}", headers=admin)
    assert got.json()["agentId"] is None


# ---- stats ----


async def test_agent_stats_slice(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, agent = await tenant_and_login(client, platform_headers, create_tenant_user, Role.AGENT)
    admin = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.ADMIN, email="admin@a.example.com"
    )
    profile = await make_profile(client, agent)
    listing = await make_listing(client, agent)
    assert (await transition(client, admin, listing["id"], "published")).status_code == 200
    captured = await capture(
        client, capture_body(email="s@example.com", listingId=listing["id"])
    )
    assert captured.status_code == 201

    stats = await client.get(f"{PORTAL_AGENTS}/{profile['id']}/stats", headers=agent)
    assert stats.status_code == 200, stats.text
    body = stats.json()
    assert body["listingsByStatus"] == {"published": 1}
    assert body["leadsByStage"] == {"new": 1}  # listing_agent default assigned it
    assert body["avgFirstResponseSeconds"] is None

    # Another agent can't read someone else's stats (404, no oracle).
    other = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="other@a.example.com"
    )
    assert (
        await client.get(f"{PORTAL_AGENTS}/{profile['id']}/stats", headers=other)
    ).status_code == 404
    # A manager can.
    assert (
        await client.get(f"{PORTAL_AGENTS}/{profile['id']}/stats", headers=admin)
    ).status_code == 200
