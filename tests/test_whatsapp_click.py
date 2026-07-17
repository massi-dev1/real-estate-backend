"""WhatsApp handoff capture (§8.6): the public widget POSTs the click, a lead
lands in the CRM with source ``whatsapp_click``, and the response carries the
prefilled wa.me deep link. Number resolution: listing agent's profile number
first, tenant ``settings.contact.whatsapp_number`` second, loud 409 when
neither is configured."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from httpx import AsyncClient

from app.core.permissions import Role
from tests.helpers import HOST_A
from tests.test_agents import PORTAL_AGENTS, make_profile
from tests.test_leads import PORTAL_LEADS, portal_leads
from tests.test_listings import add_user, make_listing, tenant_and_login, transition

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

CLICK_URL = "/api/v1/leads/capture/whatsapp-click"


def click_body(**overrides: Any) -> dict[str, Any]:
    return {
        "contact": {"firstName": "Sam", "email": "clicker@example.com"},
        "renderedAt": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        **overrides,
    }


async def click(
    client: AsyncClient, body: dict[str, Any], *, host: str = HOST_A
) -> httpx.Response:
    return await client.post(CLICK_URL, json=body, headers={"Host": host})


async def set_tenant_whatsapp(
    client: AsyncClient, platform_headers: dict[str, str], tenant_id: str, number: str
) -> None:
    resp = await client.patch(
        f"/api/v1/platform/tenants/{tenant_id}",
        json={"settings": {"contact": {"whatsapp_number": number}}},
        headers=platform_headers,
    )
    assert resp.status_code == 200, resp.text


async def test_click_uses_listing_agents_number_and_creates_lead(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="wa-agent@a.example.com"
    )
    agent_id = (await client.get("/api/v1/users/me", headers=agent)).json()["id"]
    await make_profile(client, agent, whatsappNumber="+213661234567")

    listing = await make_listing(client, admin, agentId=agent_id)
    assert (await transition(client, admin, listing["id"], "published")).status_code == 200

    resp = await click(client, click_body(listingId=listing["id"], message="Is it available?"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["whatsappUrl"].startswith("https://wa.me/213661234567?text=")
    # The prefill names the listing so the agent's WhatsApp shows intent.
    assert listing["referenceCode"] in body["whatsappUrl"]

    lead = await client.get(f"{PORTAL_LEADS}/{body['id']}", headers=admin)
    assert lead.status_code == 200, lead.text
    assert lead.json()["source"] == "whatsapp_click"
    assert lead.json()["listingId"] == listing["id"]
    # The default listing_agent strategy assigned the click to the agent.
    assert lead.json()["agentId"] == agent_id


async def test_click_falls_back_to_tenant_number(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    # Free-form JSONB settings: punctuation must be stripped, not rejected.
    await set_tenant_whatsapp(client, platform_headers, str(tenant["id"]), "+213 555 00 11 22")

    resp = await click(client, click_body())
    assert resp.status_code == 201, resp.text
    assert resp.json()["whatsappUrl"].startswith("https://wa.me/213555001122?text=")

    items = await portal_leads(client, admin)
    assert len(items) == 1
    assert items[0]["source"] == "whatsapp_click"
    assert items[0]["listingId"] is None


async def test_click_409_when_no_number_configured(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await click(client, click_body())
    assert resp.status_code == 409, resp.text
    # Failing loudly must not half-create the lead.
    assert await portal_leads(client, admin) == []


async def test_click_honeypot_returns_real_url_but_persists_nothing(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    await set_tenant_whatsapp(client, platform_headers, str(tenant["id"]), "+213770000000")

    resp = await click(client, click_body(hp="gotcha"))
    # A bot sees a perfectly normal response, working link included...
    assert resp.status_code == 201
    assert resp.json()["whatsappUrl"].startswith("https://wa.me/213770000000?text=")
    # ...but nothing was persisted.
    assert await portal_leads(client, admin) == []


async def test_profile_whatsapp_number_must_be_e164(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, agent = await tenant_and_login(client, platform_headers, create_tenant_user, Role.AGENT)
    resp = await client.post(
        PORTAL_AGENTS,
        json={"slug": "bad-number", "whatsappNumber": "0661 23 45 67"},
        headers=agent,
    )
    assert resp.status_code == 422

    profile = await make_profile(client, agent, whatsappNumber="+213661112233")
    assert profile["whatsappNumber"] == "+213661112233"
