"""Content CMS (§8.10, slice 1): structured pages + versioned legal pages.

Covers portal page CRUD and the draft/published lifecycle, locale-negotiated
public serving, HMAC preview tokens for drafts, legal-page versioning (each
edit a new row, one current per kind), RBAC gating, sitemap inclusion, and
tenant isolation.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from httpx import AsyncClient

from app.core.permissions import Role
from tests.helpers import HOST_A, HOST_B
from tests.test_listings import add_user, tenant_and_login
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

PORTAL_PAGES = "/api/v1/portal/content/pages"
PORTAL_LEGAL = "/api/v1/portal/content/legal"

PAGE_BODY: dict[str, Any] = {
    "slug": "about-us",
    "title": {"fr": "À propos", "en": "About us"},
    "blocks": [
        {"type": "hero", "data": {"heading": "Bienvenue"}},
        {"type": "richtext", "data": {"html": "<p>Notre agence.</p>"}},
    ],
    "seoMeta": {"title": {"fr": "À propos | Agence"}, "description": {"fr": "Qui nous sommes."}},
}


async def make_page(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    resp = await client.post(PORTAL_PAGES, json={**PAGE_BODY, **overrides}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_page_crud_lifecycle(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    page = await make_page(client, admin)
    assert page["status"] == "draft"
    assert page["publishedAt"] is None

    # Duplicate slug → 409.
    dup = await client.post(PORTAL_PAGES, json=PAGE_BODY, headers=admin)
    assert dup.status_code == 409, dup.text

    # Fetch + patch.
    got = await client.get(f"{PORTAL_PAGES}/{page['id']}", headers=admin)
    assert got.status_code == 200
    patched = await client.patch(
        f"{PORTAL_PAGES}/{page['id']}",
        json={"title": {"fr": "À propos de nous", "en": "About us"}},
        headers=admin,
    )
    assert patched.status_code == 200
    assert patched.json()["title"]["fr"] == "À propos de nous"

    # Publish stamps publishedAt; unpublish keeps it but flips status.
    published = await client.post(f"{PORTAL_PAGES}/{page['id']}/publish", headers=admin)
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert published.json()["publishedAt"] is not None

    unpublished = await client.post(f"{PORTAL_PAGES}/{page['id']}/unpublish", headers=admin)
    assert unpublished.status_code == 200
    assert unpublished.json()["status"] == "draft"

    # Delete → gone.
    deleted = await client.delete(f"{PORTAL_PAGES}/{page['id']}", headers=admin)
    assert deleted.status_code == 204
    missing = await client.get(f"{PORTAL_PAGES}/{page['id']}", headers=admin)
    assert missing.status_code == 404


async def test_public_page_draft_hidden_published_negotiated(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.MARKETING)
    page = await make_page(client, admin)

    # Draft is invisible to the public.
    draft = await client.get("/api/v1/pages/about-us", headers={"Host": HOST_A})
    assert draft.status_code == 404

    await client.post(f"{PORTAL_PAGES}/{page['id']}/publish", headers=admin)

    # Published: one negotiated locale per i18n field.
    en = await client.get(
        "/api/v1/pages/about-us", headers={"Host": HOST_A, "Accept-Language": "en"}
    )
    assert en.status_code == 200
    body = en.json()
    assert body["title"] == "About us"
    # No en SEO title, so the fallback chain (fr→en→ar) fills it from fr —
    # an i18n field with any translation never resolves to a hole.
    assert body["seoTitle"] == "À propos | Agence"
    assert len(body["blocks"]) == 2

    fr = await client.get("/api/v1/pages/about-us?locale=fr", headers={"Host": HOST_A})
    assert fr.json()["title"] == "À propos"
    assert fr.json()["seoTitle"] == "À propos | Agence"


async def test_preview_token_exposes_draft(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    page = await make_page(client, admin)

    token_resp = await client.post(f"{PORTAL_PAGES}/{page['id']}/preview-token", headers=admin)
    assert token_resp.status_code == 200
    token = token_resp.json()["token"]

    # A valid token reaches the unpublished draft.
    ok = await client.get(f"/api/v1/pages/about-us/preview?token={token}", headers={"Host": HOST_A})
    assert ok.status_code == 200
    assert ok.json()["title"] == "À propos"

    # A forged token 404s (no oracle).
    forged = await client.get(
        "/api/v1/pages/about-us/preview?token=about-us.deadbeef", headers={"Host": HOST_A}
    )
    assert forged.status_code == 404

    # A valid token but wrong slug 404s too.
    wrong_slug = await client.get(
        f"/api/v1/pages/other/preview?token={token}", headers={"Host": HOST_A}
    )
    assert wrong_slug.status_code == 404


async def test_preview_token_does_not_cross_tenants(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin_a = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await create_tenant(client, platform_headers, name="Agency B", slug="agency-b", domain=HOST_B)

    page = await make_page(client, admin_a)
    token = (
        await client.post(f"{PORTAL_PAGES}/{page['id']}/preview-token", headers=admin_a)
    ).json()["token"]

    # Tenant A's token replayed against tenant B's host → 404.
    cross = await client.get(
        f"/api/v1/pages/about-us/preview?token={token}", headers={"Host": HOST_B}
    )
    assert cross.status_code == 404


async def test_legal_versioning(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)

    v1 = await client.post(
        PORTAL_LEGAL,
        json={"kind": "privacy", "body": {"fr": "Politique v1"}},
        headers=admin,
    )
    assert v1.status_code == 201, v1.text
    assert v1.json()["version"] == 1
    assert v1.json()["isCurrent"] is True

    v2 = await client.post(
        PORTAL_LEGAL,
        json={"kind": "privacy", "body": {"fr": "Politique v2"}},
        headers=admin,
    )
    assert v2.status_code == 201
    assert v2.json()["version"] == 2

    # Public serves the current (v2) version.
    public = await client.get("/api/v1/legal/privacy?locale=fr", headers={"Host": HOST_A})
    assert public.status_code == 200
    assert public.json()["version"] == 2
    assert public.json()["body"] == "Politique v2"

    # History lists both, newest first, exactly one current.
    history = await client.get(f"{PORTAL_LEGAL}/privacy/history", headers=admin)
    rows = history.json()
    assert [r["version"] for r in rows] == [2, 1]
    assert sum(1 for r in rows if r["isCurrent"]) == 1

    # Footer index lists the one current privacy row.
    index = await client.get("/api/v1/legal", headers={"Host": HOST_A})
    assert index.status_code == 200
    assert [r["kind"] for r in index.json()] == ["privacy"]

    # A kind never published → 404.
    missing = await client.get("/api/v1/legal/terms", headers={"Host": HOST_A})
    assert missing.status_code == 404


async def test_content_rbac(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    # An agent has no CONTENT_MANAGE.
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="agent@a.example.com"
    )
    forbidden = await client.post(PORTAL_PAGES, json=PAGE_BODY, headers=agent)
    assert forbidden.status_code == 403

    # Marketing can.
    marketing = await add_user(
        client,
        create_tenant_user,
        str(tenant["id"]),
        Role.MARKETING,
        email="marketing@a.example.com",
    )
    allowed = await client.post(
        PORTAL_PAGES, json={**PAGE_BODY, "slug": "buyers"}, headers=marketing
    )
    assert allowed.status_code == 201


async def test_sitemap_includes_published_pages(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    published = await make_page(client, admin, slug="sellers")
    await client.post(f"{PORTAL_PAGES}/{published['id']}/publish", headers=admin)
    await make_page(client, admin, slug="hidden-draft")  # stays draft

    sitemap = await client.get("/api/v1/sitemap.xml", headers={"Host": HOST_A})
    assert sitemap.status_code == 200
    xml = sitemap.text
    assert "/pages/sellers" in xml
    assert "/pages/hidden-draft" not in xml


async def test_page_isolated_across_tenants(
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
    page = await make_page(client, admin_a)

    # Tenant B cannot read tenant A's page by id (404, no oracle).
    cross = await client.get(f"{PORTAL_PAGES}/{page['id']}", headers=admin_b)
    assert cross.status_code == 404

    # Tenant B may reuse the same slug — the unique constraint is per-tenant.
    reused = await client.post(PORTAL_PAGES, json=PAGE_BODY, headers=admin_b)
    assert reused.status_code == 201
