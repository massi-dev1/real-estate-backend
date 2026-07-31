"""Background workers (§12): email delivery task and the listing-expiry Beat
job. Celery runs in eager mode (conftest) so ``.delay()`` executes inline
against the real Postgres/Redis stack — no separate worker process needed.
"""

import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

from fastapi import FastAPI, Request
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session, on_commit, set_tenant_guc
from app.core.permissions import Role
from app.workers.db import _run_scoped
from app.workers.tasks.email import send_email
from app.workers.tasks.listings import flag_stale_listings
from tests.helpers import mailpit_code
from tests.test_listings import make_listing, tenant_and_login, transition
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]


async def test_send_email_task_delivers(app: FastAPI) -> None:
    to = "worker-task@example.com"
    send_email.delay(to=to, subject="Task delivery check", text="code: worker-ok\n")
    code = await mailpit_code(to, "Task")
    assert code == "worker-ok"


async def test_flag_stale_listings_flags_past_expiry(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin, expiresAt="2020-01-01T00:00:00Z")
    resp = await transition(client, admin, listing["id"], "published")
    assert resp.status_code == 200, resp.text

    total = flag_stale_listings()
    assert total == 1

    got = await client.get(f"/api/v1/portal/listings/{listing['id']}", headers=admin)
    assert got.json()["staleFlaggedAt"] is not None


async def test_flag_stale_listings_uses_age_fallback_without_expires_at(
    app: FastAPI,
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)  # no expiresAt
    resp = await transition(client, admin, listing["id"], "published")
    assert resp.status_code == 200, resp.text

    # Backdate published_at past the staleness threshold directly — the API
    # has no way to set a historical publish date. listings is RLS-protected,
    # so the write needs the tenant GUC set just like the request path does.
    old = datetime.now(UTC) - timedelta(days=200)
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, uuid.UUID(tenant["id"]))
        await session.execute(
            text("UPDATE listings SET published_at = :old WHERE id = :id"),
            {"old": old, "id": listing["id"]},
        )

    total = flag_stale_listings()
    assert total == 1

    got = await client.get(f"/api/v1/portal/listings/{listing['id']}", headers=admin)
    assert got.json()["staleFlaggedAt"] is not None


async def test_flag_stale_listings_skips_recent_and_unpublished(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    published_recent = await make_listing(client, admin)
    resp = await transition(client, admin, published_recent["id"], "published")
    assert resp.status_code == 200, resp.text
    await make_listing(client, admin)  # stays draft

    total = flag_stale_listings()
    assert total == 0


async def test_flag_stale_listings_is_idempotent_across_runs(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin, expiresAt="2020-01-01T00:00:00Z")
    resp = await transition(client, admin, listing["id"], "published")
    assert resp.status_code == 200, resp.text

    assert flag_stale_listings() == 1
    # Already-flagged listings are excluded, so a second run (retry, overlap)
    # must not re-count them.
    assert flag_stale_listings() == 0


async def test_editing_a_flagged_listing_clears_the_flag(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin, expiresAt="2020-01-01T00:00:00Z")
    resp = await transition(client, admin, listing["id"], "published")
    assert resp.status_code == 200, resp.text
    assert flag_stale_listings() == 1

    patched = await client.patch(
        f"/api/v1/portal/listings/{listing['id']}", json={"beds": 4}, headers=admin
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["staleFlaggedAt"] is None


async def test_flag_stale_listings_is_tenant_scoped(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin_a = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing_a = await make_listing(client, admin_a, expiresAt="2020-01-01T00:00:00Z")
    assert (await transition(client, admin_a, listing_a["id"], "published")).status_code == 200

    tenant_b = await create_tenant(
        client, platform_headers, name="Agency B", slug="agency-b", domain="agency-b.test"
    )
    await create_tenant_user(str(tenant_b["id"]), "admin@b.example.com", Role.ADMIN)
    admin_b_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@b.example.com", "password": "Fixture-Pass-123456"},
        headers={"Host": "agency-b.test"},
    )
    assert admin_b_login.status_code == 200, admin_b_login.text
    admin_b = {
        "Host": "agency-b.test",
        "Authorization": f"Bearer {admin_b_login.json()['accessToken']}",
    }
    listing_b = await make_listing(client, admin_b, expiresAt="2020-01-01T00:00:00Z")
    assert (await transition(client, admin_b, listing_b["id"], "published")).status_code == 200

    total = flag_stale_listings()
    assert total == 2  # both tenants' stale listings get flagged in one run

    got_a = await client.get(f"/api/v1/portal/listings/{listing_a['id']}", headers=admin_a)
    got_b = await client.get(f"/api/v1/portal/listings/{listing_b['id']}", headers=admin_b)
    assert got_a.json()["staleFlaggedAt"] is not None
    assert got_b.json()["staleFlaggedAt"] is not None


# ----------------------------- post-commit callback isolation (COD-01) ---


async def test_worker_drain_runs_every_callback_even_when_one_raises() -> None:
    """A failing post-commit callback must not skip the ones queued after it.

    These are independent side effects of an already-committed transaction, so
    dropping the remainder would silently leave a partially-invalidated cache or
    an unqueued job. Matters most here: a Beat sweep registers callbacks for
    many rows in a single drain.
    """
    ran: list[str] = []

    async def ok_first() -> None:
        ran.append("first")

    async def boom() -> None:
        ran.append("boom")
        raise ConnectionError("redis is down")

    async def ok_last() -> None:
        ran.append("last")

    async def body(session: AsyncSession) -> None:
        for cb in (ok_first, boom, ok_last):
            on_commit(session, cb)

    # Unscoped: the callbacks are the subject, no tenant rows are touched.
    await _run_scoped(None, body)

    assert ran == ["first", "boom", "last"], f"a raising callback stopped the drain: {ran}"


async def test_request_drain_runs_every_callback_even_when_one_raises(app: FastAPI) -> None:
    """Same isolation on the request path, driven through the real
    ``get_session`` dependency rather than a reimplementation of its drain."""
    ran: list[str] = []

    async def ok_first() -> None:
        ran.append("first")

    async def boom() -> None:
        raise ConnectionError("redis is down")

    async def ok_last() -> None:
        ran.append("last")

    class _StubRequest:
        def __init__(self, application: FastAPI) -> None:
            self.app = application
            self.state = SimpleNamespace()  # no tenant → unscoped session

    agen = get_session(cast(Request, _StubRequest(app)))
    session = await anext(agen)
    for cb in (ok_first, boom, ok_last):
        on_commit(session, cb)
    with suppress(StopAsyncIteration):
        await anext(agen)  # closes the session, commits, drains callbacks

    assert ran == ["first", "last"], f"a raising callback stopped the drain: {ran}"
