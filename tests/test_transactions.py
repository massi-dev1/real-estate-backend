"""Transactions & deals module (§8.13/§13): deal CRUD + workflow, milestone
checklist (seeded + ad hoc), the commission admin-only gate, private-bucket
document upload (presign → PUT → confirm with server-computed sha256) +
presigned download, ownership scoping / visibility, the milestone-reminder
Beat sweep routed through notify(), and tenant isolation.
"""

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update

from app.core.database import set_tenant_guc
from app.core.permissions import Role
from app.modules.transactions.models import DealMilestone
from app.workers.tasks.transactions import send_milestone_reminders
from tests.helpers import HOST_B
from tests.test_leads import mailpit_count
from tests.test_listings import add_user, make_listing, tenant_and_login
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

PORTAL_DEALS = "/api/v1/portal/deals"


async def _user_id(client: AsyncClient, headers: dict[str, str]) -> uuid.UUID:
    resp = await client.get("/api/v1/users/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["id"])


async def make_deal(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    body = {"title": "123 Main St sale", **overrides}
    resp = await client.post(PORTAL_DEALS, json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


# ---- deal CRUD + workflow ----


async def test_create_deal_seeds_milestones(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    deal = await make_deal(client, admin, price="15000000.00")
    assert deal["status"] == "open"
    assert Decimal(deal["price"]) == Decimal("15000000")

    milestones = (await client.get(f"{PORTAL_DEALS}/{deal['id']}/milestones", headers=admin)).json()
    assert len(milestones) == 5  # the default checklist
    assert milestones[0]["title"] == "Offer accepted"
    assert milestones[0]["completedAt"] is None


async def test_create_deal_without_seed(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    deal = await make_deal(client, admin, seed_milestones=False)
    milestones = (await client.get(f"{PORTAL_DEALS}/{deal['id']}/milestones", headers=admin)).json()
    assert milestones == []


async def test_deal_links_validated(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    # A bogus listing link is a 404, not an FK 500.
    resp = await client.post(
        PORTAL_DEALS,
        json={"title": "x", "listingId": str(uuid.uuid4())},
        headers=admin,
    )
    assert resp.status_code == 404, resp.text

    # A real listing link is accepted.
    listing = await make_listing(client, admin)
    deal = await make_deal(client, admin, listingId=listing["id"])
    assert deal["listingId"] == listing["id"]


async def test_deal_workflow_transitions(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    deal = await make_deal(client, admin)

    async def move(to: str, **extra: Any) -> httpx.Response:
        return await client.post(
            f"{PORTAL_DEALS}/{deal['id']}/status", json={"toStatus": to, **extra}, headers=admin
        )

    assert (await move("under_contract")).json()["status"] == "under_contract"
    won = await move("closed_won")
    assert won.json()["status"] == "closed_won"
    assert won.json()["closedAt"] is not None

    # A closed deal is terminal — no further moves.
    assert (await move("open")).status_code == 409


async def test_closed_lost_requires_reason(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    deal = await make_deal(client, admin)
    missing = await client.post(
        f"{PORTAL_DEALS}/{deal['id']}/status", json={"toStatus": "closed_lost"}, headers=admin
    )
    assert missing.status_code == 409
    ok = await client.post(
        f"{PORTAL_DEALS}/{deal['id']}/status",
        json={"toStatus": "closed_lost", "lostReason": "financing fell through"},
        headers=admin,
    )
    assert ok.status_code == 200
    assert ok.json()["lostReason"] == "financing fell through"


async def test_same_status_transition_is_idempotent(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    deal = await make_deal(client, admin)
    resp = await client.post(
        f"{PORTAL_DEALS}/{deal['id']}/status", json={"toStatus": "open"}, headers=admin
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "open"


# ---- ownership scoping / visibility ----


async def test_agent_sees_only_own_deals(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    agent_a = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="a-agent@a.example.com"
    )
    agent_b = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="b-agent@a.example.com"
    )
    a_deal = await make_deal(client, agent_a, title="A's deal")

    # Agent B can't see or fetch A's deal (404 — no existence oracle).
    b_list = (await client.get(PORTAL_DEALS, headers=agent_b)).json()
    assert a_deal["id"] not in {d["id"] for d in b_list["items"]}
    assert (await client.get(f"{PORTAL_DEALS}/{a_deal['id']}", headers=agent_b)).status_code == 404

    # Admin sees it tenant-wide.
    admin_get = await client.get(f"{PORTAL_DEALS}/{a_deal['id']}", headers=admin)
    assert admin_get.status_code == 200


async def test_agent_cannot_assign_deal_to_another(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="scoped@a.example.com"
    )
    other_id = await _user_id(client, admin)
    resp = await client.post(
        PORTAL_DEALS, json={"title": "x", "ownerUserId": str(other_id)}, headers=agent
    )
    assert resp.status_code == 403

    # An admin can assign a deal to another owner.
    agent_id = await _user_id(client, agent)
    assigned = await make_deal(client, admin, ownerUserId=str(agent_id))
    assert assigned["ownerUserId"] == str(agent_id)


async def test_bogus_owner_id_is_clean_404(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """An admin-supplied owner that isn't a real tenant user is a clean 404, not
    an FK IntegrityError → 500 (same guard as the CRM-link validation)."""
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    # Nonexistent user id on create.
    resp = await client.post(
        PORTAL_DEALS, json={"title": "x", "ownerUserId": str(uuid.uuid4())}, headers=admin
    )
    assert resp.status_code == 404, resp.text

    # And on a milestone owner.
    deal = await make_deal(client, admin, seed_milestones=False)
    mresp = await client.post(
        f"{PORTAL_DEALS}/{deal['id']}/milestones",
        json={"title": "m", "ownerUserId": str(uuid.uuid4())},
        headers=admin,
    )
    assert mresp.status_code == 404, mresp.text


async def test_foreign_tenant_owner_rejected(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """A user from another tenant can't be assigned as a deal owner (would leak
    across the tenant boundary and 500 on the RESTRICT FK)."""
    _, admin_a = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, email="fa@a.example.com"
    )
    tenant_b = await create_tenant(
        client, platform_headers, name="Agency B", slug="agency-b", domain=HOST_B
    )
    admin_b = await add_user(
        client,
        create_tenant_user,
        str(tenant_b["id"]),
        Role.ADMIN,
        email="fb@b.example.com",
        host=HOST_B,
    )
    b_user_id = await _user_id(client, admin_b)
    resp = await client.post(
        PORTAL_DEALS, json={"title": "x", "ownerUserId": str(b_user_id)}, headers=admin_a
    )
    assert resp.status_code == 404, resp.text


async def test_marketing_has_no_deal_access(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, _admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    marketing = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.MARKETING, email="mkt@a.example.com"
    )
    # Marketing lacks DEAL_MANAGE entirely — commissions are sensitive.
    assert (await client.get(PORTAL_DEALS, headers=marketing)).status_code == 403


# ---- commissions (admin-only) ----


async def test_commission_gate(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="comm@a.example.com"
    )
    agent_id = await _user_id(client, agent)
    deal = await make_deal(client, admin, ownerUserId=str(agent_id), price="20000000.00")

    # An agent's deal view carries no commission keys at all.
    agent_view = (await client.get(f"{PORTAL_DEALS}/{deal['id']}", headers=agent)).json()
    assert "commissionAmount" not in agent_view

    # An agent cannot read or set the commission.
    assert (
        await client.get(f"{PORTAL_DEALS}/{deal['id']}/commission", headers=agent)
    ).status_code == 403
    assert (
        await client.put(
            f"{PORTAL_DEALS}/{deal['id']}/commission",
            json={"basis": "percentage", "rate": "2.5"},
            headers=agent,
        )
    ).status_code == 403

    # An admin sets a percentage commission — the amount is derived from price.
    set_resp = await client.put(
        f"{PORTAL_DEALS}/{deal['id']}/commission",
        json={"basis": "percentage", "rate": "2.5"},
        headers=admin,
    )
    assert set_resp.status_code == 200
    body = set_resp.json()
    assert Decimal(body["commissionRate"]) == Decimal("2.5")
    assert Decimal(body["commissionAmount"]) == Decimal("500000")  # 20,000,000 * 2.5%

    # An admin's deal view now carries the commission figures.
    admin_view = (await client.get(f"{PORTAL_DEALS}/{deal['id']}", headers=admin)).json()
    assert Decimal(admin_view["commissionAmount"]) == Decimal("500000")


async def test_flat_commission(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    deal = await make_deal(client, admin)
    resp = await client.put(
        f"{PORTAL_DEALS}/{deal['id']}/commission",
        json={"basis": "flat", "amount": "350000.00"},
        headers=admin,
    )
    assert resp.status_code == 200
    assert Decimal(resp.json()["commissionAmount"]) == Decimal("350000")
    assert resp.json()["commissionRate"] is None


# ---- milestones ----


async def test_milestone_add_complete_delete(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    deal = await make_deal(client, admin, seed_milestones=False)

    created = await client.post(
        f"{PORTAL_DEALS}/{deal['id']}/milestones",
        json={"title": "Inspection", "dueDate": "2026-08-01"},
        headers=admin,
    )
    assert created.status_code == 201
    milestone_id = created.json()["id"]

    done = await client.patch(
        f"{PORTAL_DEALS}/{deal['id']}/milestones/{milestone_id}",
        json={"completed": True},
        headers=admin,
    )
    assert done.status_code == 200
    assert done.json()["completedAt"] is not None

    deleted = await client.delete(
        f"{PORTAL_DEALS}/{deal['id']}/milestones/{milestone_id}", headers=admin
    )
    assert deleted.status_code == 204
    remaining = (await client.get(f"{PORTAL_DEALS}/{deal['id']}/milestones", headers=admin)).json()
    assert remaining == []


# ---- documents (private bucket, sha256) ----


async def test_document_upload_confirm_download(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    deal = await make_deal(client, admin)
    data = b"%PDF-1.4 fake signed contract bytes"

    presign = await client.post(
        f"{PORTAL_DEALS}/{deal['id']}/documents/uploads",
        json={"docType": "contract", "filename": "contract.pdf", "contentType": "application/pdf"},
        headers=admin,
    )
    assert presign.status_code == 201, presign.text
    body = presign.json()
    doc_id = body["document"]["id"]
    assert body["document"]["status"] == "pending"

    async with httpx.AsyncClient() as direct:
        put = await direct.put(body["uploadUrl"], content=data, headers=body["headers"])
        assert put.status_code in (200, 204), put.text

    confirm = await client.post(
        f"{PORTAL_DEALS}/{deal['id']}/documents/{doc_id}/confirm", headers=admin
    )
    assert confirm.status_code == 200, confirm.text
    confirmed = confirm.json()
    assert confirmed["status"] == "ready"
    # sha256 is computed server-side, never trusted from the client.
    assert confirmed["sha256"] == hashlib.sha256(data).hexdigest()
    assert confirmed["sizeBytes"] == len(data)

    # A presigned download URL returns the exact bytes.
    dl = await client.get(f"{PORTAL_DEALS}/{deal['id']}/documents/{doc_id}/download", headers=admin)
    assert dl.status_code == 200
    async with httpx.AsyncClient() as direct:
        fetched = await direct.get(dl.json()["url"])
        assert fetched.status_code == 200
        assert fetched.content == data


async def test_confirm_without_upload_fails(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    deal = await make_deal(client, admin)
    presign = await client.post(
        f"{PORTAL_DEALS}/{deal['id']}/documents/uploads",
        json={"docType": "misc", "filename": "x.pdf", "contentType": "application/pdf"},
        headers=admin,
    )
    doc_id = presign.json()["document"]["id"]
    # No PUT happened — confirm finds no object and marks the doc failed (409).
    confirm = await client.post(
        f"{PORTAL_DEALS}/{deal['id']}/documents/{doc_id}/confirm", headers=admin
    )
    assert confirm.status_code == 409


# ---- milestone reminder Beat sweep (via notify) ----


async def _age_milestone_due(app: FastAPI, tenant_id: uuid.UUID, milestone_id: uuid.UUID) -> None:
    """Backdate a milestone's due_date to yesterday so the sweep picks it up."""
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tenant_id)
        await session.execute(
            update(DealMilestone).where(DealMilestone.id == milestone_id).values(due_date=yesterday)
        )


async def test_milestone_reminder_notifies_owner(
    app: FastAPI,
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, email="owner@a.example.com"
    )
    tenant_id = uuid.UUID(str(tenant["id"]))
    owner_id = await _user_id(client, admin)
    deal = await make_deal(client, admin, seed_milestones=False)
    created = await client.post(
        f"{PORTAL_DEALS}/{deal['id']}/milestones",
        json={"title": "Closing", "dueDate": "2030-01-01"},
        headers=admin,
    )
    milestone_id = uuid.UUID(created.json()["id"])
    await _age_milestone_due(app, tenant_id, milestone_id)

    before = await mailpit_count("owner@a.example.com", "échéance")
    result = send_milestone_reminders()
    assert result["reminders_sent"] >= 1

    # In-app notification landed for the owner.
    notes = (await client.get("/api/v1/me/notifications", headers=admin)).json()
    types = {n["type"] for n in notes["items"]}
    assert "milestone_due" in types

    # And the email went out (default channel), rendered in the owner's locale.
    after = await mailpit_count("owner@a.example.com", "échéance")
    assert after > before

    # Idempotent: a second run finds nothing due (reminder_sent_at stamped).
    second = send_milestone_reminders()
    assert second["reminders_sent"] == 0
    _ = owner_id


# ---- tenant isolation ----


async def test_deal_tenant_isolation(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin_a = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, email="iso-a@a.example.com"
    )
    deal_a = await make_deal(client, admin_a, title="Tenant A deal")

    tenant_b = await create_tenant(
        client, platform_headers, name="Agency B", slug="agency-b", domain=HOST_B
    )
    admin_b = await add_user(
        client,
        create_tenant_user,
        str(tenant_b["id"]),
        Role.ADMIN,
        email="iso-admin@b.example.com",
        host=HOST_B,
    )

    # Tenant B's admin cannot fetch tenant A's deal (cross-tenant → 404).
    resp = await client.get(f"{PORTAL_DEALS}/{deal_a['id']}", headers=admin_b)
    assert resp.status_code == 404
