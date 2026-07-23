"""Analytics & reporting module (§8.15/§13): anonymous typed event ingestion,
the nightly rollup Beat job (raw events + leads → the three daily rollup
tables), the dashboards (which read only from rollups), the ANALYTICS_VIEW gate,
per-listing report visibility scoping, partition maintenance (create-ahead +
prune-by-drop), and tenant isolation.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.permissions import Role
from app.modules.analytics.service import (
    _month_partition,
    _partition_month_end,
)
from app.workers.tasks.analytics import (
    ensure_analytics_partitions,
    prune_analytics_events,
    rollup_analytics,
)
from tests.helpers import HOST_A, HOST_B
from tests.test_leads import capture, capture_body
from tests.test_listings import add_user, make_listing, tenant_and_login, transition
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

EVENTS_URL = "/api/v1/analytics/events"
PORTAL = "/api/v1/portal/analytics"


def _rendered() -> str:
    return (datetime.now(UTC) - timedelta(seconds=30)).isoformat()


async def _publish_listing(
    client: AsyncClient, headers: dict[str, str], **overrides: object
) -> str:
    listing = await make_listing(client, headers, **overrides)
    resp = await transition(client, headers, listing["id"], "published")
    assert resp.status_code == 200, resp.text
    return str(listing["id"])


async def _post_events(
    client: AsyncClient, events: list[dict], *, host: str = HOST_A
) -> None:
    resp = await client.post(EVENTS_URL, json={"events": events}, headers={"Host": host})
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] == len(events)


# ---- ingestion ----


async def test_ingest_typed_events(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await create_tenant(client, platform_headers, name="Agency A", slug="agency-a")
    await _post_events(
        client,
        [
            {"eventType": "page_view", "path": "/home"},
            {"eventType": "listing_view", "listingId": str(uuid.uuid4())},
            {"eventType": "search", "query": "algiers", "resultsCount": 12},
        ],
    )


async def test_ingest_rejects_unknown_type(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await create_tenant(client, platform_headers, name="Agency A", slug="agency-a")
    resp = await client.post(
        EVENTS_URL,
        json={"events": [{"eventType": "hack", "foo": "bar"}]},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 422, resp.text


async def test_ingest_rejects_arbitrary_payload_fields(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    """A page_view carrying a listing_view's fields (or any extra key) is a 422 —
    anonymous clients cannot smuggle arbitrary JSON (§8.15)."""
    await create_tenant(client, platform_headers, name="Agency A", slug="agency-a")
    resp = await client.post(
        EVENTS_URL,
        json={"events": [{"eventType": "page_view", "evil": "x"}]},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 422, resp.text


async def test_ingest_listing_view_requires_listing(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await create_tenant(client, platform_headers, name="Agency A", slug="agency-a")
    resp = await client.post(
        EVENTS_URL,
        json={"events": [{"eventType": "listing_view"}]},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 422, resp.text


async def test_ingest_empty_and_oversized_batch(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await create_tenant(client, platform_headers, name="Agency A", slug="agency-a")
    empty = await client.post(EVENTS_URL, json={"events": []}, headers={"Host": HOST_A})
    assert empty.status_code == 422, empty.text
    big = await client.post(
        EVENTS_URL,
        json={"events": [{"eventType": "page_view"} for _ in range(51)]},
        headers={"Host": HOST_A},
    )
    assert big.status_code == 422, big.text


# ---- rollups + dashboards ----


async def test_rollup_and_traffic_dashboard(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing_id = await _publish_listing(client, admin)

    # Two views, one save, one inquiry for the listing today.
    await _post_events(
        client,
        [
            {"eventType": "listing_view", "listingId": listing_id},
            {"eventType": "listing_view", "listingId": listing_id},
            {"eventType": "favorite", "listingId": listing_id},
            {"eventType": "form_submit", "listingId": listing_id, "form": "contact"},
        ],
    )
    rollup_analytics()

    traffic = (await client.get(f"{PORTAL}/traffic", headers=admin)).json()
    assert traffic["totalViews"] == 2
    assert traffic["totalSaves"] == 1
    assert traffic["totalInquiries"] == 1
    assert len(traffic["series"]) == 1
    assert traffic["series"][0]["views"] == 2

    top = (await client.get(f"{PORTAL}/top-listings", headers=admin)).json()
    assert len(top) == 1
    assert top[0]["listingId"] == listing_id
    assert top[0]["views"] == 2


async def test_rollup_is_idempotent(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing_id = await _publish_listing(client, admin)
    await _post_events(client, [{"eventType": "listing_view", "listingId": listing_id}])

    rollup_analytics()
    rollup_analytics()  # a re-run must not double-count (upsert on natural key)

    traffic = (await client.get(f"{PORTAL}/traffic", headers=admin)).json()
    assert traffic["totalViews"] == 1


async def test_lead_funnel_and_source_dashboards(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    # Three captured leads today, two sources.
    r1 = await capture(client, capture_body(email="a@x.com", source="listing_form"))
    assert r1.status_code == 201, r1.text
    r2 = await capture(client, capture_body(email="b@x.com", source="listing_form"))
    assert r2.status_code == 201
    r3 = await capture(client, capture_body(email="c@x.com", source="phone"))
    assert r3.status_code == 201

    # Mark one lead won so conversion is non-zero.
    await _set_lead_won(client, admin, r1.json()["id"])

    rollup_analytics()

    funnel = (await client.get(f"{PORTAL}/lead-funnel", headers=admin)).json()
    assert funnel["totalCreated"] == 3
    assert funnel["totalWon"] == 1
    assert funnel["conversionRate"] == round(1 / 3, 4)

    sources = (await client.get(f"{PORTAL}/sources", headers=admin)).json()
    by_source = {s["source"]: s for s in sources}
    assert by_source["listing_form"]["leadsCreated"] == 2
    assert by_source["listing_form"]["leadsWon"] == 1
    assert by_source["phone"]["leadsCreated"] == 1
    assert by_source["phone"]["leadsWon"] == 0


async def test_listing_performance_scoped_to_agent(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="agent@a.example.com"
    )
    agent_id = str((await client.get("/api/v1/users/me", headers=agent)).json()["id"])
    # Admin owns one listing; the agent owns another (assigned to them by the
    # admin — agents can't self-publish, but ownership scoping keys on agent_id).
    admin_listing = await _publish_listing(client, admin)
    agent_listing = await _publish_listing(client, admin, agent_id=agent_id)
    await _post_events(
        client,
        [
            {"eventType": "listing_view", "listingId": admin_listing},
            {"eventType": "listing_view", "listingId": agent_listing},
            {"eventType": "listing_view", "listingId": agent_listing},
        ],
    )
    rollup_analytics()

    # The agent's per-listing report shows only their own listing.
    agent_report = (await client.get(f"{PORTAL}/listing-performance", headers=agent)).json()
    ids = {row["listingId"] for row in agent_report["listings"]}
    assert ids == {agent_listing}
    assert agent_report["listings"][0]["views"] == 2

    # The admin sees both (tenant-wide).
    admin_report = (await client.get(f"{PORTAL}/listing-performance", headers=admin)).json()
    admin_ids = {row["listingId"] for row in admin_report["listings"]}
    assert admin_ids == {admin_listing, agent_listing}


# ---- RBAC + isolation ----


async def test_dashboards_require_permission(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    buyer = await add_user(
        client,
        create_tenant_user,
        str(tenant["id"]),
        Role.BUYER_RENTER,
        email="buyer@a.example.com",
    )
    # The tenant-wide aggregate dashboards need ANALYTICS_VIEW.
    for path in ("traffic", "top-listings", "lead-funnel", "sources"):
        resp = await client.get(f"{PORTAL}/{path}", headers=buyer)
        assert resp.status_code == 403, f"{path}: {resp.text}"
    # The per-listing report is authorization-by-ownership (no ANALYTICS_VIEW):
    # a buyer owns no listings, so it's an allowed-but-empty 200, not a 403.
    report = await client.get(f"{PORTAL}/listing-performance", headers=buyer)
    assert report.status_code == 200, report.text
    assert report.json()["listings"] == []


async def test_rollup_tenant_isolation(
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
        host=HOST_B,
        email="admin@b.example.com",
    )
    listing_a = await _publish_listing(client, admin_a)
    await _post_events(client, [{"eventType": "listing_view", "listingId": listing_a}])
    rollup_analytics()

    # B's dashboard is empty — A's events never leak across the tenant boundary.
    traffic_b = (await client.get(f"{PORTAL}/traffic", headers=admin_b)).json()
    assert traffic_b["totalViews"] == 0
    top_b = (await client.get(f"{PORTAL}/top-listings", headers=admin_b)).json()
    assert top_b == []


# ---- partition maintenance ----


async def test_ensure_partitions_creates_future_months(app: FastAPI) -> None:
    created_before = await _partition_names(app)
    ensure_analytics_partitions()
    created_after = await _partition_names(app)
    # The next lookahead months now exist (idempotent — a re-run adds nothing).
    assert created_after >= created_before
    now = datetime.now(UTC)
    name, _, _ = _month_partition(now.year, now.month)
    assert name in created_after
    again = ensure_analytics_partitions()
    assert again == []  # nothing new the second time


async def test_prune_drops_old_partitions(app: FastAPI) -> None:
    # Create a partition for a month well past the 90-day retention window. DDL
    # needs the postgres role (app_user can't CREATE on public), same as the
    # maintenance task.
    old = date.today() - timedelta(days=400)
    name, start, end = _month_partition(old.year, old.month)
    ddl = create_async_engine(get_settings().database_ddl_url)
    try:
        async with ddl.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF analytics_events "
                    f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
                )
            )
    finally:
        await ddl.dispose()
    assert name in await _partition_names(app)

    dropped = prune_analytics_events()
    assert name in dropped
    assert name not in await _partition_names(app)


def test_partition_month_end_parsing() -> None:
    assert _partition_month_end("analytics_events_2026_07") == date(2026, 8, 1)
    assert _partition_month_end("analytics_events_2026_12") == date(2027, 1, 1)
    assert _partition_month_end("not_a_partition") is None


# ---- helpers ----


async def _set_lead_won(client: AsyncClient, headers: dict[str, str], lead_id: str) -> None:
    resp = await client.post(
        f"/api/v1/portal/leads/{lead_id}/stage",
        json={"toStage": "won"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


async def _partition_names(app: FastAPI) -> set[str]:
    async with app.state.engine.begin() as conn:
        rows = await conn.execute(
            text(
                "SELECT inhrelid::regclass::text AS name FROM pg_inherits "
                "WHERE inhparent = 'analytics_events'::regclass"
            )
        )
        return {r.name for r in rows}
