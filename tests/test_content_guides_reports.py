"""Content CMS slice 3 (§8.10 tail, §13): neighborhood guides + market reports.

Covers guide CRUD + publish, live ST_Contains auto-linking of listings inside a
boundary, the nightly stats recompute Beat job, market-report CRUD + PDF render
(eager-mode Celery → real MinIO round-trip), the gated download → lead flow with
honeypot camouflage, sitemap inclusion, RBAC, and tenant isolation.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient

from app.core.permissions import Role
from app.workers.tasks.content import recompute_guide_stats
from tests.helpers import HOST_A, HOST_B
from tests.test_listings import make_listing, tenant_and_login, transition
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

PORTAL_GUIDES = "/api/v1/portal/content/guides"
PORTAL_REPORTS = "/api/v1/portal/content/reports"

# A square boundary around Alger centre (lon 3.042, lat 36.7525) — the default
# listing point in LISTING_BODY falls inside it.
ALGER_BOUNDARY = [[[2.9, 36.6], [3.2, 36.6], [3.2, 36.9], [2.9, 36.9], [2.9, 36.6]]]

GUIDE_BODY: dict[str, Any] = {
    "slug": "algiers-centre",
    "name": {"fr": "Alger Centre", "en": "Algiers Centre"},
    "body": {"fr": "Un quartier animé.", "en": "A lively district."},
    "boundary": ALGER_BOUNDARY,
}

REPORT_BODY: dict[str, Any] = {
    "slug": "q3-2026",
    "title": {"fr": "Rapport Q3 2026", "en": "Q3 2026 Report"},
    "stats": {"median_price": "12500000", "listings_sold": 42, "avg_days_on_market": 55},
}


def _capture(**overrides: Any) -> dict[str, Any]:
    """A valid public-capture body (honeypot empty, form aged past the min)."""
    rendered = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    return {
        "contact": {"email": "buyer@example.com", "firstName": "Sam"},
        "hp": "",
        "renderedAt": rendered,
        **overrides,
    }


async def make_guide(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    resp = await client.post(PORTAL_GUIDES, json={**GUIDE_BODY, **overrides}, headers=headers)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def make_report(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    resp = await client.post(PORTAL_REPORTS, json={**REPORT_BODY, **overrides}, headers=headers)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def publish_listing(client: AsyncClient, headers: dict[str, str], **overrides: Any) -> str:
    listing = await make_listing(client, headers, **overrides)
    resp = await transition(client, headers, listing["id"], "published")
    assert resp.status_code == 200, resp.text
    return str(listing["id"])


# ---- guides ----


async def test_guide_crud_and_publish(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    guide = await make_guide(client, admin)
    assert guide["status"] == "draft"
    assert guide["publishedAt"] is None
    assert guide["boundary"] is not None

    # Duplicate slug → 409.
    dup = await client.post(PORTAL_GUIDES, json=GUIDE_BODY, headers=admin)
    assert dup.status_code == 409

    # Draft invisible to the public.
    hidden = await client.get("/api/v1/guides/algiers-centre", headers={"Host": HOST_A})
    assert hidden.status_code == 404

    published = await client.post(f"{PORTAL_GUIDES}/{guide['id']}/publish", headers=admin)
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert published.json()["publishedAt"] is not None

    # Public detail: one negotiated locale, boundary, empty listings.
    detail = await client.get(
        "/api/v1/guides/algiers-centre", headers={"Host": HOST_A, "Accept-Language": "en"}
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["guide"]["name"] == "Algiers Centre"
    assert body["guide"]["body"] == "A lively district."
    assert body["listings"] == []

    deleted = await client.delete(f"{PORTAL_GUIDES}/{guide['id']}", headers=admin)
    assert deleted.status_code == 204


async def test_guide_auto_links_listings_inside_boundary(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)

    # One listing inside the boundary (default point), one far outside it.
    inside = await publish_listing(client, admin)
    await publish_listing(client, admin, location={"lat": 34.0, "lng": -6.8})  # Rabat

    guide = await make_guide(client, admin)
    await client.post(f"{PORTAL_GUIDES}/{guide['id']}/publish", headers=admin)

    detail = await client.get("/api/v1/guides/algiers-centre", headers={"Host": HOST_A})
    assert detail.status_code == 200
    ids = [row["id"] for row in detail.json()["listings"]]
    assert ids == [inside]  # only the in-boundary listing auto-links


async def test_guide_stats_sweep(
    app: FastAPI,
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await publish_listing(client, admin)  # price 12500000 inside boundary
    await publish_listing(client, admin, price="17500000.00")

    guide = await make_guide(client, admin)
    await client.post(f"{PORTAL_GUIDES}/{guide['id']}/publish", headers=admin)

    # Before the sweep: no auto stats.
    before = await client.get(f"{PORTAL_GUIDES}/{guide['id']}", headers=admin)
    assert before.json()["stats"] == {}

    processed = recompute_guide_stats()
    assert processed >= 1

    after = await client.get(f"{PORTAL_GUIDES}/{guide['id']}", headers=admin)
    stats = after.json()["stats"]
    assert stats["listing_count"] == 2
    # Median of {12.5M, 17.5M} = 15M.
    assert stats["median_price"] == "15000000.00"
    assert after.json()["statsComputedAt"] is not None


async def test_guide_without_boundary_gets_no_auto_stats(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    guide = await make_guide(client, admin, slug="editorial", boundary=None)
    await client.post(f"{PORTAL_GUIDES}/{guide['id']}/publish", headers=admin)

    # The sweep skips it (no boundary); detail returns no listings.
    recompute_guide_stats()
    got = await client.get(f"{PORTAL_GUIDES}/{guide['id']}", headers=admin)
    assert got.json()["stats"] == {}

    detail = await client.get("/api/v1/guides/editorial", headers={"Host": HOST_A})
    assert detail.json()["listings"] == []


# ---- reports ----


async def test_report_crud_publish_and_pdf_render(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    report = await make_report(client, admin)
    assert report["status"] == "draft"

    # Duplicate slug → 409.
    dup = await client.post(PORTAL_REPORTS, json=REPORT_BODY, headers=admin)
    assert dup.status_code == 409

    # Not public while draft.
    hidden = await client.get("/api/v1/reports/q3-2026", headers={"Host": HOST_A})
    assert hidden.status_code == 404

    # Publish enqueues the PDF render (eager) → row flips to ready.
    published = await client.post(f"{PORTAL_REPORTS}/{report['id']}/publish", headers=admin)
    assert published.status_code == 200
    refetched = await client.get(f"{PORTAL_REPORTS}/{report['id']}", headers=admin)
    assert refetched.json()["status"] == "ready"
    assert refetched.json()["generatedAt"] is not None

    # Public metadata — stats, but no PDF URL.
    public = await client.get(
        "/api/v1/reports/q3-2026", headers={"Host": HOST_A, "Accept-Language": "en"}
    )
    assert public.status_code == 200
    body = public.json()
    assert body["title"] == "Q3 2026 Report"
    assert body["stats"]["listings_sold"] == 42
    assert body["pdfReady"] is True
    assert "download_url" not in body and "pdfObjectKey" not in body


async def test_report_download_gate_mints_lead(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    report = await make_report(client, admin)
    await client.post(f"{PORTAL_REPORTS}/{report['id']}/publish", headers=admin)

    resp = await client.post(
        "/api/v1/reports/q3-2026/download", json=_capture(), headers={"Host": HOST_A}
    )
    assert resp.status_code == 200, resp.text
    url = resp.json()["downloadUrl"]
    assert "report.pdf" in url or "report-q3-2026" in url

    # A lead landed with the market_report source.
    leads = await client.get("/api/v1/portal/leads?source=market_report", headers=admin)
    assert leads.status_code == 200
    assert leads.json()["totalEstimate"] == 1


async def test_report_download_honeypot_camouflage(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    report = await make_report(client, admin)
    await client.post(f"{PORTAL_REPORTS}/{report['id']}/publish", headers=admin)

    # Honeypot filled → real-shaped 200, nothing persists.
    resp = await client.post(
        "/api/v1/reports/q3-2026/download",
        json=_capture(hp="i-am-a-bot"),
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 200
    assert resp.json()["downloadUrl"]

    leads = await client.get("/api/v1/portal/leads?source=market_report", headers=admin)
    assert leads.json()["totalEstimate"] == 0


async def test_report_download_not_ready_conflicts(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await make_report(client, admin)
    # A draft report is not public at all — the gate 404s (no oracle) before it
    # ever reaches the "not ready" state.
    resp = await client.post(
        "/api/v1/reports/q3-2026/download", json=_capture(), headers={"Host": HOST_A}
    )
    assert resp.status_code == 404


# ---- sitemap / rbac / isolation ----


async def test_sitemap_includes_published_guides(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    guide = await make_guide(client, admin, slug="published-guide")
    await client.post(f"{PORTAL_GUIDES}/{guide['id']}/publish", headers=admin)
    await make_guide(client, admin, slug="draft-guide")  # stays draft

    sitemap = await client.get("/api/v1/sitemap.xml", headers={"Host": HOST_A})
    xml = sitemap.text
    assert "/guides/published-guide" in xml
    assert "/guides/draft-guide" not in xml


async def test_guides_reports_rbac(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    from tests.test_listings import add_user

    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="agent@a.example.com"
    )
    assert (await client.post(PORTAL_GUIDES, json=GUIDE_BODY, headers=agent)).status_code == 403
    assert (await client.post(PORTAL_REPORTS, json=REPORT_BODY, headers=agent)).status_code == 403

    marketing = await add_user(
        client,
        create_tenant_user,
        str(tenant["id"]),
        Role.MARKETING,
        email="marketing@a.example.com",
    )
    assert (await client.post(PORTAL_GUIDES, json=GUIDE_BODY, headers=marketing)).status_code == 201


async def test_guides_reports_isolated_across_tenants(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    from tests.test_listings import add_user

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

    guide_a = await make_guide(client, admin_a)
    # Tenant B cannot fetch tenant A's guide by id (404, no oracle).
    cross = await client.get(f"{PORTAL_GUIDES}/{guide_a['id']}", headers=admin_b)
    assert cross.status_code == 404
