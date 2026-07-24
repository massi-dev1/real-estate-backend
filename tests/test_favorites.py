"""Favorites & saved searches (§8.9/§13): idempotent favorites, dashboard
cards, saved-search CRUD + ownership, anonymous double-opt-in → lead, instant
publish alerts, digest watermark, unsubscribe."""

import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select

from app.core.database import set_tenant_guc
from app.core.permissions import Role
from app.modules.favorites.models import SavedSearch
from app.workers.tasks.favorites import send_saved_search_digests
from tests.helpers import HOST_A, MAILPIT_URL, bearer, mailpit_code, register_user
from tests.test_leads import mailpit_count
from tests.test_listings import make_listing, tenant_and_login, transition

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

FAVORITES = "/api/v1/me/favorites"
SAVED = "/api/v1/me/saved-searches"
SIGNUP = "/api/v1/saved-searches"


async def buyer_headers(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await register_user(client, HOST_A, email=email)
    assert resp.status_code == 201, resp.text
    return {"Host": HOST_A, "Authorization": bearer(resp)}


async def published_listing(
    client: AsyncClient, admin: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    listing = await make_listing(client, admin, **overrides)
    resp = await transition(client, admin, listing["id"], "published")
    assert resp.status_code == 200, resp.text
    return listing


def signup_body(email: str, **overrides: Any) -> dict[str, Any]:
    return {
        "email": email,
        "renderedAt": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        **overrides,
    }


async def saved_search_count(app: FastAPI, tenant_id: str) -> int:
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, uuid.UUID(tenant_id))
        stmt = select(func.count()).select_from(SavedSearch)
        return int((await session.execute(stmt)).scalar_one())


async def mailpit_text(to: str, subject_word: str) -> str:
    async with httpx.AsyncClient() as mailpit:
        resp = await mailpit.get(
            f"{MAILPIT_URL}/api/v1/search",
            params={"query": f"to:{to} subject:{subject_word}"},
        )
        resp.raise_for_status()
        messages = resp.json()["messages"]
        assert messages, f"no '{subject_word}' email delivered to {to}"
        resp = await mailpit.get(f"{MAILPIT_URL}/api/v1/message/{messages[0]['ID']}")
        resp.raise_for_status()
        return str(resp.json()["Text"])


# ---- favorites ----


async def test_favorite_put_idempotent_and_listed(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await published_listing(client, admin)
    buyer = await buyer_headers(client, "fav-buyer@example.com")

    first = await client.put(f"{FAVORITES}/{listing['id']}", headers=buyer)
    second = await client.put(f"{FAVORITES}/{listing['id']}", headers=buyer)
    assert first.status_code == second.status_code == 204

    resp = await client.get(FAVORITES, headers=buyer)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1  # double-tap did not duplicate
    assert items[0]["listing"]["id"] == listing["id"]
    assert items[0]["listing"]["title"] == "Bel appartement F3"  # negotiated locale
    assert items[0]["favoritedAt"] is not None

    gone = await client.delete(f"{FAVORITES}/{listing['id']}", headers=buyer)
    again = await client.delete(f"{FAVORITES}/{listing['id']}", headers=buyer)
    assert gone.status_code == again.status_code == 204
    resp = await client.get(FAVORITES, headers=buyer)
    assert resp.json()["items"] == []


async def test_favorite_requires_published_listing(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    draft = await make_listing(client, admin)
    buyer = await buyer_headers(client, "fav-draft@example.com")

    resp = await client.put(f"{FAVORITES}/{draft['id']}", headers=buyer)
    assert resp.status_code == 404  # drafts are not public inventory
    resp = await client.put(f"{FAVORITES}/{uuid.uuid4()}", headers=buyer)
    assert resp.status_code == 404


async def test_favorites_list_drops_unpublished(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await published_listing(client, admin)
    buyer = await buyer_headers(client, "fav-unpub@example.com")
    assert (await client.put(f"{FAVORITES}/{listing['id']}", headers=buyer)).status_code == 204

    resp = await transition(client, admin, listing["id"], "archived")
    assert resp.status_code == 200, resp.text

    resp = await client.get(FAVORITES, headers=buyer)
    assert resp.status_code == 200
    assert resp.json()["items"] == []  # the row survives, the card drops out


async def test_favorites_require_auth(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.get(FAVORITES, headers={"Host": HOST_A})
    assert resp.status_code == 401


# ---- saved searches (/me) ----


async def test_saved_search_crud(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    buyer = await buyer_headers(client, "ss-crud@example.com")

    created = await client.post(
        SAVED,
        json={
            "name": "Apartments in Alger",
            "filters": {"city": "Alger", "priceMax": "20000000", "purpose": "sale"},
            "frequency": "daily",
        },
        headers=buyer,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["filters"]["city"] == "Alger"  # the validated camelCase dump
    assert body["frequency"] == "daily"
    assert body["isActive"] is True
    search_id = body["id"]

    # Unknown feature never reaches the JSONB column.
    bad = await client.post(
        SAVED, json={"name": "x", "filters": {"features": ["jacuzzi-lava"]}}, headers=buyer
    )
    assert bad.status_code == 422

    patched = await client.patch(
        f"{SAVED}/{search_id}", json={"name": "Renamed", "frequency": "instant"}, headers=buyer
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["frequency"] == "instant"

    # Explicit null for a NOT NULL field is rejected, not a 500 at flush.
    nulled = await client.patch(f"{SAVED}/{search_id}", json={"name": None}, headers=buyer)
    assert nulled.status_code == 422

    listed = await client.get(SAVED, headers=buyer)
    assert [s["id"] for s in listed.json()] == [search_id]

    assert (await client.delete(f"{SAVED}/{search_id}", headers=buyer)).status_code == 204
    assert (await client.get(f"{SAVED}/{search_id}", headers=buyer)).status_code == 404


async def test_saved_search_ownership_no_oracle(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    owner = await buyer_headers(client, "ss-owner@example.com")
    other = await buyer_headers(client, "ss-other@example.com")

    created = await client.post(SAVED, json={"name": "Mine"}, headers=owner)
    assert created.status_code == 201
    search_id = created.json()["id"]

    assert (await client.get(f"{SAVED}/{search_id}", headers=other)).status_code == 404
    assert (
        await client.patch(f"{SAVED}/{search_id}", json={"name": "Stolen"}, headers=other)
    ).status_code == 404
    assert (await client.delete(f"{SAVED}/{search_id}", headers=other)).status_code == 404


async def test_saved_search_cap(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    buyer = await buyer_headers(client, "ss-cap@example.com")
    for i in range(20):
        resp = await client.post(SAVED, json={"name": f"s{i}"}, headers=buyer)
        assert resp.status_code == 201, resp.text
    resp = await client.post(SAVED, json={"name": "one too many"}, headers=buyer)
    assert resp.status_code == 409


# ---- anonymous signup, double-opt-in, lead conversion ----


async def test_anonymous_signup_confirm_creates_lead(
    app: FastAPI,
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    email = "anon-signup@example.com"
    resp = await client.post(
        SIGNUP,
        json=signup_body(email, filters={"city": "Alger"}, frequency="weekly"),
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 201, resp.text

    token = await mailpit_code(email, "Confirm")
    confirmed = await client.post(
        f"{SIGNUP}/confirm", json={"token": token}, headers={"Host": HOST_A}
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["isActive"] is True

    # The consumed token is single-use.
    replay = await client.post(f"{SIGNUP}/confirm", json={"token": token}, headers={"Host": HOST_A})
    assert replay.status_code == 401

    # The opt-in was the capture: a search_signup lead now exists.
    leads = await client.get("/api/v1/portal/leads", headers=admin)
    assert leads.status_code == 200
    items = leads.json()["items"]
    assert len(items) == 1
    assert items[0]["source"] == "search_signup"
    detail = await client.get(f"/api/v1/portal/leads/{items[0]['id']}", headers=admin)
    assert detail.json()["contact"]["email"] == email
    assert detail.json()["contact"]["consent"]["marketing_email"] is True

    assert await saved_search_count(app, str(tenant["id"])) == 1


async def test_signup_honeypot_persists_nothing(
    app: FastAPI,
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.post(
        SIGNUP,
        json=signup_body("bot@example.com", hp="gotcha"),
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 201  # real-shaped response, no signal to the bot
    assert await saved_search_count(app, str(tenant["id"])) == 0
    assert await mailpit_count("bot@example.com", "Confirm") == 0


async def test_signup_too_fast_is_rejected(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    body = signup_body("fast@example.com")
    body["renderedAt"] = datetime.now(UTC).isoformat()
    resp = await client.post(SIGNUP, json=body, headers={"Host": HOST_A})
    assert resp.status_code == 422


# ---- instant alerts on publish ----


async def test_instant_alert_on_matching_publish(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    match_email = "alert-match@example.com"
    miss_email = "alert-miss@example.com"
    matcher = await buyer_headers(client, match_email)
    misser = await buyer_headers(client, miss_email)

    resp = await client.post(
        SAVED,
        json={"name": "Alger flats", "filters": {"city": "Alger"}, "frequency": "instant"},
        headers=matcher,
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        SAVED,
        json={"name": "Oran flats", "filters": {"city": "Oran"}, "frequency": "instant"},
        headers=misser,
    )
    assert resp.status_code == 201, resp.text

    # Mailpit accumulates across test runs — assert deltas, not absolutes.
    match_before = await mailpit_count(match_email, "listing")
    miss_before = await mailpit_count(miss_email, "listing")

    # Publishing fires the post-commit matcher (eager Celery in tests).
    await published_listing(client, admin)

    assert await mailpit_count(match_email, "listing") == match_before + 1
    assert await mailpit_count(miss_email, "listing") == miss_before

    text = await mailpit_text(match_email, "listing")
    assert "Alger flats" in text
    assert "unsubscribe:" in text


# ---- digests ----


async def test_digest_sends_once_per_watermark(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    email = "digest-buyer@example.com"
    buyer = await buyer_headers(client, email)

    resp = await client.post(
        SAVED,
        json={"name": "Daily Alger", "filters": {"city": "Alger"}, "frequency": "daily"},
        headers=buyer,
    )
    assert resp.status_code == 201, resp.text

    await published_listing(client, admin)

    before = await mailpit_count(email, "Daily")
    send_saved_search_digests()
    assert await mailpit_count(email, "Daily") == before + 1

    # The watermark advanced — a rerun with nothing new sends nothing.
    send_saved_search_digests()
    assert await mailpit_count(email, "Daily") == before + 1


# ---- unsubscribe ----


async def test_unsubscribe_deactivates_and_rejects_forgery(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    email = "unsub-buyer@example.com"
    buyer = await buyer_headers(client, email)

    created = await client.post(
        SAVED,
        json={"name": "Unsub Alger", "filters": {"city": "Alger"}, "frequency": "instant"},
        headers=buyer,
    )
    assert created.status_code == 201
    search_id = created.json()["id"]

    before = await mailpit_count(email, "listing")
    await published_listing(client, admin)
    assert await mailpit_count(email, "listing") == before + 1
    text = await mailpit_text(email, "listing")
    match = re.search(r"unsubscribe: (\S+)", text)
    assert match, "alert email did not carry an unsubscribe token"
    token = match.group(1)

    resp = await client.post(
        f"{SIGNUP}/unsubscribe", json={"token": token}, headers={"Host": HOST_A}
    )
    assert resp.status_code == 204
    got = await client.get(f"{SAVED}/{search_id}", headers=buyer)
    assert got.json()["isActive"] is False

    # Idempotent; forged signatures are rejected.
    resp = await client.post(
        f"{SIGNUP}/unsubscribe", json={"token": token}, headers={"Host": HOST_A}
    )
    assert resp.status_code == 204
    forged = await client.post(
        f"{SIGNUP}/unsubscribe",
        json={"token": f"{search_id}.deadbeef"},
        headers={"Host": HOST_A},
    )
    assert forged.status_code == 401

    # Deactivated searches no longer alert.
    await published_listing(client, admin)
    assert await mailpit_count(email, "listing") == before + 1
