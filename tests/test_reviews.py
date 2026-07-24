"""Reviews module (§8.11/§13): moderated public submission (spam defense +
honeypot camouflage), the pending→approved|rejected moderation queue with RBAC
gating, per-agent and agency-wide public feeds + aggregates, aggregation folded
into the agent profile/stats, and tenant isolation.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from httpx import AsyncClient

from app.core.permissions import Role
from tests.helpers import HOST_A, HOST_B
from tests.test_agents import make_profile, publish_profile
from tests.test_listings import add_user, make_listing, tenant_and_login, transition
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

SUBMIT_URL = "/api/v1/reviews"
PORTAL_REVIEWS = "/api/v1/portal/reviews"


def review_body(**overrides: Any) -> dict[str, Any]:
    return {
        "rating": 5,
        "title": "Great experience",
        "body": "Sam was professional and responsive throughout.",
        "authorName": "Happy Client",
        "authorEmail": "client@example.com",
        "renderedAt": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        **overrides,
    }


async def submit_review(
    client: AsyncClient, body: dict[str, Any], *, host: str = HOST_A
) -> httpx.Response:
    return await client.post(SUBMIT_URL, json=body, headers={"Host": host})


async def moderate(
    client: AsyncClient,
    headers: dict[str, str],
    review_id: str,
    status: str,
    **extra: Any,
) -> httpx.Response:
    return await client.post(
        f"{PORTAL_REVIEWS}/{review_id}/moderate",
        json={"status": status, **extra},
        headers=headers,
    )


async def published_agent(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """An admin + a published agent profile (slug from PROFILE_BODY)."""
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="agent@a.example.com"
    )
    profile = await make_profile(client, agent)
    await publish_profile(client, admin, profile["id"])
    return tenant, admin, profile


# ---- public submission ----


async def test_submit_lands_pending(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, _, profile = await published_agent(client, platform_headers, create_tenant_user)
    resp = await submit_review(client, review_body(agentSlug=profile["slug"]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert uuid.UUID(body["id"])
    # Not visible anywhere public until approved.
    feed = await client.get(f"/api/v1/agents/{profile['slug']}/reviews", headers={"Host": HOST_A})
    assert feed.status_code == 200
    assert feed.json()["items"] == []


async def test_submit_agency_wide(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await submit_review(client, review_body())  # no agentSlug
    assert resp.status_code == 201, resp.text


async def test_submit_unknown_agent_404(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await submit_review(client, review_body(agentSlug="no-such-agent"))
    assert resp.status_code == 404, resp.text


async def test_submit_unpublished_agent_404(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """A draft (unpublished) profile can't receive reviews — the slug resolver
    only matches published profiles."""
    tenant, _admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="agent@a.example.com"
    )
    profile = await make_profile(client, agent)  # not published
    resp = await submit_review(client, review_body(agentSlug=profile["slug"]))
    assert resp.status_code == 404, resp.text


async def test_submit_with_listing_context(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _tenant, admin, profile = await published_agent(client, platform_headers, create_tenant_user)
    listing = await make_listing(client, admin)
    await transition(client, admin, listing["id"], "published")
    resp = await submit_review(
        client, review_body(agentSlug=profile["slug"], listingRef=listing["referenceCode"])
    )
    assert resp.status_code == 201, resp.text


async def test_submit_bad_listing_ref_404(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await submit_review(client, review_body(listingRef="AGE-2026-99999"))
    assert resp.status_code == 404, resp.text


async def test_rating_out_of_range_422(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    for bad in (0, 6):
        resp = await submit_review(client, review_body(rating=bad))
        assert resp.status_code == 422, resp.text


# ---- spam defense ----


async def test_honeypot_camouflage(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """A honeypot hit gets a real-shaped 201 (fabricated id) and persists
    nothing — no queue entry appears for the moderator."""
    _, admin, _ = await published_agent(client, platform_headers, create_tenant_user)
    resp = await submit_review(client, review_body(hp="i-am-a-bot"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "pending"

    listed = await client.get(PORTAL_REVIEWS, headers=admin)
    assert listed.status_code == 200
    assert listed.json()["items"] == []


async def test_too_fast_rejected(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    body = review_body(renderedAt=datetime.now(UTC).isoformat())
    resp = await submit_review(client, body)
    assert resp.status_code == 422, resp.text


async def test_stale_form_rejected(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    body = review_body(renderedAt=(datetime.now(UTC) - timedelta(days=2)).isoformat())
    resp = await submit_review(client, body)
    assert resp.status_code == 422, resp.text


# ---- moderation queue + RBAC ----


async def test_moderation_requires_permission(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """An agent (no REVIEW_MODERATE) cannot see or moderate the queue."""
    tenant, _admin, profile = await published_agent(client, platform_headers, create_tenant_user)
    submit = await submit_review(client, review_body(agentSlug=profile["slug"]))
    review_id = submit.json()["id"]
    agent = await add_user(
        client,
        create_tenant_user,
        str(tenant["id"]),
        Role.AGENT,
        email="agent@a.example.com",
        existing=True,
    )
    listed = await client.get(PORTAL_REVIEWS, headers=agent)
    assert listed.status_code == 403
    denied = await moderate(client, agent, review_id, "approved")
    assert denied.status_code == 403


async def test_approve_shows_in_feed_and_aggregate(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, profile = await published_agent(client, platform_headers, create_tenant_user)
    submit = await submit_review(client, review_body(agentSlug=profile["slug"], rating=4))
    review_id = submit.json()["id"]

    approved = await moderate(client, admin, review_id, "approved", isVerified=True)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["isVerified"] is True
    assert approved.json()["moderatedBy"] is not None

    feed = await client.get(f"/api/v1/agents/{profile['slug']}/reviews", headers={"Host": HOST_A})
    items = feed.json()["items"]
    assert len(items) == 1
    assert items[0]["rating"] == 4
    assert items[0]["isVerified"] is True
    # No email leaks to the public feed.
    assert "authorEmail" not in items[0]

    # Aggregate on the public profile.
    detail = await client.get(f"/api/v1/agents/{profile['slug']}", headers={"Host": HOST_A})
    assert detail.json()["reviews"] == {"count": 1, "average": 4.0}


async def test_reject_stays_hidden(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, profile = await published_agent(client, platform_headers, create_tenant_user)
    submit = await submit_review(client, review_body(agentSlug=profile["slug"]))
    review_id = submit.json()["id"]
    rejected = await moderate(client, admin, review_id, "rejected", note="spam")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    feed = await client.get(f"/api/v1/agents/{profile['slug']}/reviews", headers={"Host": HOST_A})
    assert feed.json()["items"] == []
    detail = await client.get(f"/api/v1/agents/{profile['slug']}", headers={"Host": HOST_A})
    assert detail.json()["reviews"] == {"count": 0, "average": None}


async def test_moderation_is_one_way(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """Once decided, a review can't flip approved↔rejected (409); re-applying
    the same decision is idempotent (200)."""
    _, admin, profile = await published_agent(client, platform_headers, create_tenant_user)
    submit = await submit_review(client, review_body(agentSlug=profile["slug"]))
    review_id = submit.json()["id"]

    assert (await moderate(client, admin, review_id, "approved")).status_code == 200
    # Same decision again → still 200.
    assert (await moderate(client, admin, review_id, "approved")).status_code == 200
    # Flip → 409.
    flip = await moderate(client, admin, review_id, "rejected")
    assert flip.status_code == 409, flip.text


async def test_moderate_status_pending_rejected(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, profile = await published_agent(client, platform_headers, create_tenant_user)
    submit = await submit_review(client, review_body(agentSlug=profile["slug"]))
    review_id = submit.json()["id"]
    resp = await moderate(client, admin, review_id, "pending")
    assert resp.status_code == 422, resp.text


async def test_queue_filter_by_status(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, profile = await published_agent(client, platform_headers, create_tenant_user)
    a = (await submit_review(client, review_body(agentSlug=profile["slug"]))).json()["id"]
    (await submit_review(client, review_body(agentSlug=profile["slug"], authorName="Two")))
    await moderate(client, admin, a, "approved")

    pending = await client.get(PORTAL_REVIEWS, headers=admin, params={"status": "pending"})
    assert {r["id"] for r in pending.json()["items"]} == {
        r["id"] for r in pending.json()["items"] if r["status"] == "pending"
    }
    assert all(r["status"] == "pending" for r in pending.json()["items"])
    assert len(pending.json()["items"]) == 1

    approved = await client.get(PORTAL_REVIEWS, headers=admin, params={"status": "approved"})
    assert len(approved.json()["items"]) == 1
    assert approved.json()["items"][0]["id"] == a


async def test_delete_removes_from_aggregate(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, profile = await published_agent(client, platform_headers, create_tenant_user)
    submit = await submit_review(client, review_body(agentSlug=profile["slug"]))
    review_id = submit.json()["id"]
    await moderate(client, admin, review_id, "approved")

    deleted = await client.delete(f"{PORTAL_REVIEWS}/{review_id}", headers=admin)
    assert deleted.status_code == 204
    detail = await client.get(f"/api/v1/agents/{profile['slug']}", headers={"Host": HOST_A})
    assert detail.json()["reviews"] == {"count": 0, "average": None}


# ---- agency-wide feed + summary ----


async def test_agency_feed_and_summary(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, profile = await published_agent(client, platform_headers, create_tenant_user)
    # One agency-wide (no agent) + one agent-specific; both approved.
    agency = (await submit_review(client, review_body(rating=5))).json()["id"]
    agent_rev = (
        await submit_review(client, review_body(agentSlug=profile["slug"], rating=3))
    ).json()["id"]
    await moderate(client, admin, agency, "approved")
    await moderate(client, admin, agent_rev, "approved")

    # Agency feed carries only the no-agent testimonial.
    feed = await client.get("/api/v1/reviews", headers={"Host": HOST_A})
    items = feed.json()["items"]
    assert len(items) == 1
    assert items[0]["agentUserId"] is None

    # Summary spans every approved review in the tenant (avg of 5 and 3).
    summary = await client.get("/api/v1/reviews/summary", headers={"Host": HOST_A})
    assert summary.json() == {"count": 2, "average": 4.0}


# ---- stats integration ----


async def test_stats_includes_reviews(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, profile = await published_agent(client, platform_headers, create_tenant_user)
    submit = await submit_review(client, review_body(agentSlug=profile["slug"], rating=5))
    await moderate(client, admin, submit.json()["id"], "approved")
    stats = await client.get(f"/api/v1/portal/agents/{profile['id']}/stats", headers=admin)
    assert stats.status_code == 200, stats.text
    assert stats.json()["reviews"] == {"count": 1, "average": 5.0}


# ---- tenant isolation ----


async def test_tenant_isolation(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """A review submitted on tenant A never appears in tenant B's queue or
    aggregate."""
    _, admin_a, profile = await published_agent(client, platform_headers, create_tenant_user)
    submit = await submit_review(client, review_body(agentSlug=profile["slug"]))
    review_id = submit.json()["id"]
    await moderate(client, admin_a, review_id, "approved")

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
    listed = await client.get(PORTAL_REVIEWS, headers=admin_b)
    assert listed.status_code == 200
    assert listed.json()["items"] == []

    # B can't fetch A's review by id either (404, no cross-tenant oracle).
    fetched = await client.get(f"{PORTAL_REVIEWS}/{review_id}", headers=admin_b)
    assert fetched.status_code == 404
