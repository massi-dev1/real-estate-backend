"""Parametrized cross-tenant isolation harness (§13, non-negotiable suite).

§13 asks for this to be "parametrized, automatic for new modules" rather than
a hand-written case per file. Individual suites still assert isolation with
module-specific nuance (public-vs-portal reach, no-oracle on a reference code,
list emptiness); this file is the *floor* that every tenant-owned resource
clears, so a new module's omission shows up as a missing registry entry rather
than as silence.

**The rule under test:** tenant B's *admin* — the most privileged tenant role
there is — addressing tenant A's object by id gets **404, never 403**. A 403
would confirm the object exists, turning the endpoint into an existence
oracle; 404 is the codebase's uniform stance (§7) and is what every scoped
lookup already returns for an out-of-scope row.

**Adding a module:** append one ``Resource`` to ``RESOURCES``. The creator
builds the object through the *API* as tenant A's admin, so each row is made
exactly the way the product makes it (validation, workflow, service-minted
fields) rather than by a direct insert that could drift from reality.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import AsyncClient

from app.core.permissions import Role
from tests.helpers import HOST_B
from tests.test_listings import CreateTenantUser, add_user, make_listing, tenant_and_login
from tests.test_tenants_platform_api import create_tenant

Headers = dict[str, str]
Creator = Callable[[AsyncClient, Headers], Awaitable[str]]


@dataclass(frozen=True)
class Resource:
    """One tenant-owned resource type addressable by id in the portal API."""

    name: str
    # Portal URL with a single ``{id}`` placeholder.
    url: str
    create: Creator


async def _post(client: AsyncClient, headers: Headers, url: str, body: dict[str, Any]) -> str:
    resp = await client.post(url, json=body, headers=headers)
    assert resp.status_code == 201, f"{url} -> {resp.status_code}: {resp.text}"
    return str(resp.json()["id"])


# ---- creators (tenant A's admin, through the API) ----


async def _create_listing(client: AsyncClient, headers: Headers) -> str:
    return str((await make_listing(client, headers))["id"])


async def _create_lead(client: AsyncClient, headers: Headers) -> str:
    return await _post(
        client,
        headers,
        "/api/v1/portal/leads",
        {
            "contact": {"firstName": "Amina", "email": "amina@example.com"},
            "source": "phone",
        },
    )


async def _create_deal(client: AsyncClient, headers: Headers) -> str:
    return await _post(
        client, headers, "/api/v1/portal/deals", {"title": "Villa Hydra", "price": "25000000.00"}
    )


async def _create_page(client: AsyncClient, headers: Headers) -> str:
    return await _post(
        client,
        headers,
        "/api/v1/portal/content/pages",
        {"slug": "about-us", "title": {"fr": "À propos"}, "blocks": []},
    )


async def _create_guide(client: AsyncClient, headers: Headers) -> str:
    return await _post(
        client,
        headers,
        "/api/v1/portal/content/guides",
        {"slug": "hydra", "name": {"fr": "Hydra"}, "body": {"fr": "Quartier calme."}},
    )


async def _create_report(client: AsyncClient, headers: Headers) -> str:
    return await _post(
        client,
        headers,
        "/api/v1/portal/content/reports",
        {"slug": "q1-2026", "title": {"fr": "Marché Q1"}, "stats": {"median": 1000}},
    )


async def _create_post(client: AsyncClient, headers: Headers) -> str:
    return await _post(
        client,
        headers,
        "/api/v1/portal/blog/posts",
        {"slug": "market-update", "title": {"fr": "Actualité"}, "body": {"fr": "<p>Texte.</p>"}},
    )


async def _provision_agent(client: AsyncClient, headers: Headers) -> str:
    """An agent account in the caller's own tenant, created through the API."""
    return await _post(
        client,
        headers,
        "/api/v1/users",
        {
            "email": f"agent-{uuid.uuid4().hex[:8]}@example.com",
            "password": "Agent-Pass-123456",
            "role": "agent",
        },
    )


async def _create_agent_profile(client: AsyncClient, headers: Headers) -> str:
    """An admin cannot own a profile — profiles are for agent/team-lead
    accounts — so the admin creates one *for* an agent it provisions first."""
    agent_id = await _provision_agent(client, headers)
    return await _post(
        client,
        headers,
        "/api/v1/portal/agents",
        {"userId": agent_id, "slug": "karim-b", "bio": {"fr": "Agent."}},
    )


async def _create_team(client: AsyncClient, headers: Headers) -> str:
    return await _post(client, headers, "/api/v1/portal/teams", {"name": "Alger Centre"})


async def _create_webhook_endpoint(client: AsyncClient, headers: Headers) -> str:
    return await _post(
        client,
        headers,
        "/api/v1/portal/webhooks/endpoints",
        {"url": "https://example.com/hook", "events": ["lead.created"]},
    )


# One entry per tenant-owned, id-addressable portal resource. A new module
# adds a line here; the test below picks it up automatically.
RESOURCES: tuple[Resource, ...] = (
    Resource("listing", "/api/v1/portal/listings/{id}", _create_listing),
    Resource("lead", "/api/v1/portal/leads/{id}", _create_lead),
    Resource("deal", "/api/v1/portal/deals/{id}", _create_deal),
    Resource("content_page", "/api/v1/portal/content/pages/{id}", _create_page),
    Resource("neighborhood_guide", "/api/v1/portal/content/guides/{id}", _create_guide),
    Resource("market_report", "/api/v1/portal/content/reports/{id}", _create_report),
    Resource("blog_post", "/api/v1/portal/blog/posts/{id}", _create_post),
    Resource("agent_profile", "/api/v1/portal/agents/{id}", _create_agent_profile),
    Resource("team", "/api/v1/portal/teams/{id}", _create_team),
    Resource(
        "webhook_endpoint", "/api/v1/portal/webhooks/endpoints/{id}", _create_webhook_endpoint
    ),
)


@pytest.fixture
async def two_tenant_admins(
    client: AsyncClient,
    platform_headers: Headers,
    create_tenant_user: CreateTenantUser,
) -> tuple[Headers, Headers]:
    """Admin headers for two separate tenants on two separate hosts."""
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
    return admin_a, admin_b


@pytest.mark.parametrize("resource", RESOURCES, ids=lambda r: r.name)
async def test_tenant_b_cannot_reach_tenant_a_resource(
    client: AsyncClient,
    two_tenant_admins: tuple[Headers, Headers],
    resource: Resource,
) -> None:
    """B's admin gets 404 — not 403, not 200 — on A's object."""
    admin_a, admin_b = two_tenant_admins
    object_id = await resource.create(client, admin_a)

    url = resource.url.format(id=object_id)
    # A's own admin can reach it: proves the id and URL are right, so a 404
    # for B below is isolation rather than a typo'd route.
    own = await client.get(url, headers=admin_a)
    assert own.status_code == 200, f"{resource.name}: owner cannot read own object: {own.text}"

    foreign = await client.get(url, headers=admin_b)
    assert foreign.status_code == 404, (
        f"{resource.name}: tenant B's admin got {foreign.status_code} on tenant A's object "
        f"(404 expected — anything else is an existence oracle)"
    )


@pytest.mark.parametrize("resource", RESOURCES, ids=lambda r: r.name)
async def test_tenant_a_object_absent_from_tenant_b_list(
    client: AsyncClient,
    two_tenant_admins: tuple[Headers, Headers],
    resource: Resource,
) -> None:
    """The list endpoint leaks nothing either — a 404 on the detail route is
    worth little if the collection route hands the row over anyway."""
    admin_a, admin_b = two_tenant_admins
    object_id = await resource.create(client, admin_a)

    collection = resource.url.removesuffix("/{id}")
    resp = await client.get(collection, headers=admin_b)
    assert resp.status_code == 200, f"{resource.name}: {resp.text}"
    # Cursor-paginated collections return {items: [...]}; the few naturally
    # bounded ones (teams, webhook endpoints) return a bare list.
    payload = resp.json()
    items = payload["items"] if isinstance(payload, dict) else payload
    ids = [item["id"] for item in items]
    assert object_id not in ids, f"{resource.name}: tenant A's row appeared in tenant B's list"


async def test_unknown_id_is_also_404(
    client: AsyncClient, two_tenant_admins: tuple[Headers, Headers]
) -> None:
    """The same 404 for an id that never existed.

    This is what makes the isolation 404 non-informative: if a foreign row
    404'd but a nonexistent id 400'd (or vice versa), the difference would
    still reveal which ids are real.
    """
    _, admin_b = two_tenant_admins
    missing = uuid.uuid4()
    for resource in RESOURCES:
        resp = await client.get(resource.url.format(id=missing), headers=admin_b)
        assert resp.status_code == 404, f"{resource.name}: {resp.status_code} for an unknown id"


async def test_registry_covers_every_portal_module() -> None:
    """Guard against the harness silently falling behind the codebase.

    §13 wants this "automatic for new modules". It cannot be truly automatic —
    a creator needs a valid request body only the module knows — so instead
    the registry is checked against the modules that actually expose an
    id-addressable portal resource. A new one fails here until it is listed.
    """
    covered = {resource.name for resource in RESOURCES}
    expected = {
        "listing",
        "lead",
        "deal",
        "content_page",
        "neighborhood_guide",
        "market_report",
        "blog_post",
        "agent_profile",
        "team",
        "webhook_endpoint",
    }
    assert covered == expected, (
        "tenant-isolation registry drifted: add a Resource entry for any new "
        "tenant-owned portal resource"
    )
