"""Blog (§8.10, slice 2): categories, posts with a draft/scheduled/published
lifecycle, rich-text sanitization at write time, tag/category filtering,
locale-negotiated public serving, RSS, the scheduled-publish Beat sweep,
sitemap inclusion, RBAC gating, and tenant isolation.

Celery runs in eager mode (conftest) so the sweep executes inline against the
real Postgres stack — no worker process needed.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text

from app.core.database import set_tenant_guc
from app.core.permissions import Role
from app.workers.tasks.blog import publish_scheduled_posts
from tests.helpers import HOST_A, HOST_B
from tests.test_listings import add_user, tenant_and_login
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

PORTAL_POSTS = "/api/v1/portal/blog/posts"
PORTAL_CATEGORIES = "/api/v1/portal/blog/categories"

POST_BODY: dict[str, Any] = {
    "slug": "market-trends-2026",
    "title": {"fr": "Tendances 2026", "en": "2026 Trends"},
    "excerpt": {"fr": "Un aperçu du marché."},
    "body": {"fr": "<p>Le marché <strong>évolue</strong>.</p>", "en": "<p>The market moves.</p>"},
    "tags": ["market", "trends"],
}

CATEGORY_BODY: dict[str, Any] = {
    "slug": "news",
    "name": {"fr": "Actualités", "en": "News"},
}


async def make_post(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    resp = await client.post(PORTAL_POSTS, json={**POST_BODY, **overrides}, headers=headers)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def make_category(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    resp = await client.post(
        PORTAL_CATEGORIES, json={**CATEGORY_BODY, **overrides}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


# ---- categories ----


async def test_category_crud(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    category = await make_category(client, admin)
    assert category["slug"] == "news"

    # Duplicate slug → 409.
    dup = await client.post(PORTAL_CATEGORIES, json=CATEGORY_BODY, headers=admin)
    assert dup.status_code == 409, dup.text

    patched = await client.patch(
        f"{PORTAL_CATEGORIES}/{category['id']}",
        json={"name": {"fr": "Nouvelles", "en": "News"}},
        headers=admin,
    )
    assert patched.status_code == 200
    assert patched.json()["name"]["fr"] == "Nouvelles"

    # Public category listing (negotiated locale).
    public = await client.get(
        "/api/v1/blog/categories", headers={"Host": HOST_A, "Accept-Language": "en"}
    )
    assert public.status_code == 200
    assert public.json() == [{"slug": "news", "name": "News"}]

    deleted = await client.delete(f"{PORTAL_CATEGORIES}/{category['id']}", headers=admin)
    assert deleted.status_code == 204


# ---- posts ----


async def test_post_crud_lifecycle(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.MARKETING)
    post = await make_post(client, admin)
    assert post["status"] == "draft"
    assert post["publishedAt"] is None

    # Duplicate slug → 409.
    dup = await client.post(PORTAL_POSTS, json=POST_BODY, headers=admin)
    assert dup.status_code == 409, dup.text

    published = await client.post(f"{PORTAL_POSTS}/{post['id']}/publish", headers=admin)
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert published.json()["publishedAt"] is not None

    unpublished = await client.post(f"{PORTAL_POSTS}/{post['id']}/unpublish", headers=admin)
    assert unpublished.status_code == 200
    assert unpublished.json()["status"] == "draft"

    deleted = await client.delete(f"{PORTAL_POSTS}/{post['id']}", headers=admin)
    assert deleted.status_code == 204
    missing = await client.get(f"{PORTAL_POSTS}/{post['id']}", headers=admin)
    assert missing.status_code == 404


async def test_post_bad_category_id_404(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    # An unknown category id is a 404-shaped user error, not a 500 FK error.
    resp = await client.post(
        PORTAL_POSTS, json={**POST_BODY, "categoryId": str(uuid.uuid4())}, headers=admin
    )
    assert resp.status_code == 404, resp.text


async def test_scheduled_post_requires_future_time(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)

    # SCHEDULED with no scheduledAt → 422.
    missing = await client.post(
        PORTAL_POSTS, json={**POST_BODY, "status": "scheduled"}, headers=admin
    )
    assert missing.status_code == 422, missing.text

    # SCHEDULED with a past scheduledAt → 422.
    past = await client.post(
        PORTAL_POSTS,
        json={**POST_BODY, "status": "scheduled", "scheduledAt": "2020-01-01T00:00:00Z"},
        headers=admin,
    )
    assert past.status_code == 422, past.text

    # A future scheduledAt is accepted and keeps the post out of the public feed.
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    ok = await client.post(
        PORTAL_POSTS,
        json={**POST_BODY, "status": "scheduled", "scheduledAt": future},
        headers=admin,
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["status"] == "scheduled"

    hidden = await client.get("/api/v1/blog/posts/market-trends-2026", headers={"Host": HOST_A})
    assert hidden.status_code == 404


async def test_patch_scheduled_post_rejects_past_go_live(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    post = await make_post(client, admin, status="scheduled", scheduledAt=future)

    # A partial PATCH that keeps the post SCHEDULED but moves its go-live into
    # the past must be rejected — the schema's future-time validator only sees
    # the request fields, so the service guards the resulting state (409).
    past = "2020-01-01T00:00:00Z"
    resp = await client.patch(
        f"{PORTAL_POSTS}/{post['id']}", json={"scheduledAt": past}, headers=admin
    )
    assert resp.status_code == 409, resp.text

    # A future reschedule still works.
    later = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    ok = await client.patch(
        f"{PORTAL_POSTS}/{post['id']}", json={"scheduledAt": later}, headers=admin
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "scheduled"


async def test_scheduled_publish_sweep_flips_due_posts(
    app: FastAPI,
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    post = await make_post(client, admin, status="scheduled", scheduledAt=future)

    # Backdate scheduled_at into the past directly — the API rejects a past
    # scheduledAt, and there is no other way to make a row "due". RLS-protected,
    # so the write needs the tenant GUC set like the request path.
    old = datetime.now(UTC) - timedelta(minutes=5)
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, uuid.UUID(tenant["id"]))
        await session.execute(
            text("UPDATE blog_posts SET scheduled_at = :old WHERE id = :id"),
            {"old": old, "id": post["id"]},
        )

    assert publish_scheduled_posts() == 1

    got = await client.get(f"{PORTAL_POSTS}/{post['id']}", headers=admin)
    assert got.json()["status"] == "published"
    assert got.json()["publishedAt"] is not None

    # Idempotent: a second run does not re-publish or double-count.
    assert publish_scheduled_posts() == 0


async def test_public_post_negotiated_and_only_published(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    post = await make_post(client, admin)

    # Draft is invisible.
    draft = await client.get("/api/v1/blog/posts/market-trends-2026", headers={"Host": HOST_A})
    assert draft.status_code == 404

    await client.post(f"{PORTAL_POSTS}/{post['id']}/publish", headers=admin)

    en = await client.get(
        "/api/v1/blog/posts/market-trends-2026",
        headers={"Host": HOST_A, "Accept-Language": "en"},
    )
    assert en.status_code == 200
    body = en.json()
    assert body["title"] == "2026 Trends"
    assert body["body"] == "<p>The market moves.</p>"
    # No en excerpt → fallback chain fills it from the fr excerpt.
    assert body["excerpt"] == "Un aperçu du marché."
    assert body["tags"] == ["market", "trends"]


async def test_body_sanitized_on_write(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    dirty = (
        "<p>Hello <strong>world</strong></p>"
        "<script>alert(1)</script>"
        '<img src="x" onerror="alert(1)">'
        '<a href="javascript:alert(1)">bad</a>'
    )
    post = await make_post(client, admin, body={"fr": dirty})
    stored = post["body"]["fr"]
    assert "<strong>world</strong>" in stored
    assert "<script>" not in stored
    assert "onerror" not in stored
    assert "javascript:" not in stored


async def test_tag_and_category_filtering(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    category = await make_category(client, admin)

    p1 = await make_post(client, admin, slug="p1", tags=["market"], categoryId=category["id"])
    p2 = await make_post(client, admin, slug="p2", tags=["design"])
    for p in (p1, p2):
        await client.post(f"{PORTAL_POSTS}/{p['id']}/publish", headers=admin)

    by_tag = await client.get("/api/v1/blog/posts?tag=market", headers={"Host": HOST_A})
    assert [i["slug"] for i in by_tag.json()["items"]] == ["p1"]

    by_cat = await client.get("/api/v1/blog/posts?category=news", headers={"Host": HOST_A})
    assert [i["slug"] for i in by_cat.json()["items"]] == ["p1"]
    assert by_cat.json()["items"][0]["category"] == {"slug": "news", "name": "Actualités"}


async def test_rss_feed_lists_published_only(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    published = await make_post(client, admin, slug="live-post")
    await client.post(f"{PORTAL_POSTS}/{published['id']}/publish", headers=admin)
    await make_post(client, admin, slug="draft-post")  # stays draft

    rss = await client.get("/api/v1/blog/rss.xml", headers={"Host": HOST_A})
    assert rss.status_code == 200
    assert rss.headers["content-type"].startswith("application/rss+xml")
    xml = rss.text
    assert "/blog/live-post" in xml
    assert "/blog/draft-post" not in xml
    # RFC-822 pubDate, not ISO 8601.
    assert "GMT" in xml or "+0000" in xml


async def test_sitemap_includes_published_posts(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    published = await make_post(client, admin, slug="visible-post")
    await client.post(f"{PORTAL_POSTS}/{published['id']}/publish", headers=admin)
    await make_post(client, admin, slug="hidden-post")  # stays draft

    sitemap = await client.get("/api/v1/sitemap.xml", headers={"Host": HOST_A})
    assert sitemap.status_code == 200
    xml = sitemap.text
    assert "/blog/visible-post" in xml
    assert "/blog/hidden-post" not in xml


async def test_blog_rbac(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="agent@a.example.com"
    )
    forbidden = await client.post(PORTAL_POSTS, json=POST_BODY, headers=agent)
    assert forbidden.status_code == 403

    marketing = await add_user(
        client,
        create_tenant_user,
        str(tenant["id"]),
        Role.MARKETING,
        email="marketing@a.example.com",
    )
    allowed = await client.post(PORTAL_POSTS, json=POST_BODY, headers=marketing)
    assert allowed.status_code == 201


async def test_post_isolated_across_tenants(
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
    post = await make_post(client, admin_a)

    # Tenant B cannot read tenant A's post by id (404, no oracle).
    cross = await client.get(f"{PORTAL_POSTS}/{post['id']}", headers=admin_b)
    assert cross.status_code == 404

    # Tenant B may reuse the same slug — the unique constraint is per-tenant.
    reused = await client.post(PORTAL_POSTS, json=POST_BODY, headers=admin_b)
    assert reused.status_code == 201
