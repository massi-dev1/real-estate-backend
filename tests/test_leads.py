"""Leads & CRM module (§8.4/§13): capture + spam defense, contact dedupe,
assignment engine, pipeline transitions, activities, scoring, drip sequences,
tenant isolation and ownership scoping."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update

from app.core.database import set_tenant_guc
from app.core.permissions import Role
from app.modules.leads.models import Lead, LeadDripState
from app.workers.tasks.leads import sweep_drips_and_escalations
from tests.helpers import HOST_A, HOST_B, MAILPIT_URL
from tests.test_listings import add_user, make_listing, tenant_and_login, transition
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

CAPTURE_URL = "/api/v1/leads/capture"
PORTAL_LEADS = "/api/v1/portal/leads"


def capture_body(
    *,
    email: str | None = "buyer@example.com",
    phone: str | None = None,
    source: str = "other",
    **overrides: Any,
) -> dict[str, Any]:
    contact: dict[str, Any] = {"firstName": "Sam"}
    if email is not None:
        contact["email"] = email
    if phone is not None:
        contact["phone"] = phone
    return {
        "contact": contact,
        "source": source,
        "renderedAt": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        **overrides,
    }


async def capture(
    client: AsyncClient, body: dict[str, Any], *, host: str = HOST_A
) -> httpx.Response:
    return await client.post(CAPTURE_URL, json=body, headers={"Host": host})


async def portal_leads(
    client: AsyncClient, headers: dict[str, str], **params: Any
) -> list[dict[str, Any]]:
    resp = await client.get(PORTAL_LEADS, headers=headers, params=params)
    assert resp.status_code == 200, resp.text
    return list(resp.json()["items"])


async def get_drip_state(app: FastAPI, tenant_id: str, lead_id: str) -> LeadDripState | None:
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, uuid.UUID(tenant_id))
        stmt = select(LeadDripState).where(LeadDripState.lead_id == uuid.UUID(lead_id))
        return (await session.execute(stmt)).scalar_one_or_none()


async def age_lead(app: FastAPI, tenant_id: str, lead_id: str, *, hours: int) -> None:
    """Backdate a lead's created_at so escalation cutoffs can be exercised."""
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, uuid.UUID(tenant_id))
        await session.execute(
            update(Lead)
            .where(Lead.id == uuid.UUID(lead_id))
            .values(created_at=datetime.now(UTC) - timedelta(hours=hours))
        )


async def mailpit_count(to: str, subject_word: str) -> int:
    async with httpx.AsyncClient() as mailpit:
        resp = await mailpit.get(
            f"{MAILPIT_URL}/api/v1/search",
            params={"query": f"to:{to} subject:{subject_word}"},
        )
        resp.raise_for_status()
        return len(resp.json()["messages"])


# ---- capture & spam defense ----


async def test_capture_creates_contact_and_lead(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await capture(
        client,
        capture_body(
            email="first@example.com",
            source="listing_form",
            message="Is this still available?",
            utmSource="google",
            utmCampaign="spring",
            page="/listings/AGE-2026-00001",
        ),
    )
    assert resp.status_code == 201, resp.text
    lead_id = resp.json()["id"]

    got = await client.get(f"{PORTAL_LEADS}/{lead_id}", headers=admin)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["stage"] == "new"
    assert body["source"] == "listing_form"
    assert body["sourceMeta"] == {
        "utm_source": "google",
        "utm_campaign": "spring",
        "page": "/listings/AGE-2026-00001",
    }
    assert body["contact"]["email"] == "first@example.com"
    assert body["score"] > 0

    # The capture message landed as the first (system) note on the timeline.
    acts = await client.get(f"{PORTAL_LEADS}/{lead_id}/activities", headers=admin)
    assert acts.status_code == 200
    notes = [a for a in acts.json() if a["type"] == "note"]
    assert notes and notes[0]["payload"]["text"] == "Is this still available?"
    assert notes[0]["actorId"] is None


async def test_capture_dedupes_by_email_and_merge_fills(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    first = await capture(client, capture_body(email="dup@example.com"))
    body = capture_body(email="dup@example.com", phone="+213555000111")
    body["contact"]["lastName"] = "Merged"
    second = await capture(client, body)
    assert first.status_code == second.status_code == 201

    items = await portal_leads(client, admin)
    assert len(items) == 2
    assert items[0]["contactId"] == items[1]["contactId"]  # one contact, two leads

    contact = await client.get(
        f"/api/v1/portal/contacts/{items[0]['contactId']}", headers=admin
    )
    assert contact.status_code == 200
    # Merge-fill: previously-NULL fields got the new values...
    assert contact.json()["phone"] == "+213555000111"
    assert contact.json()["lastName"] == "Merged"
    # ...but existing values were never overwritten.
    assert contact.json()["firstName"] == "Sam"


async def test_capture_dedupes_by_phone_when_no_email(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await capture(client, capture_body(email=None, phone="+213555222333"))
    await capture(client, capture_body(email=None, phone="+213555222333"))
    items = await portal_leads(client, admin)
    assert len(items) == 2
    assert items[0]["contactId"] == items[1]["contactId"]


async def test_capture_requires_email_or_phone(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await capture(client, capture_body(email=None))
    assert resp.status_code == 422


async def test_capture_honeypot_drops_silently(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await capture(client, capture_body(email="bot@example.com", hp="gotcha"))
    # A bot sees a perfectly normal response...
    assert resp.status_code == 201
    assert resp.json()["id"]
    # ...but nothing was persisted.
    assert await portal_leads(client, admin) == []


async def test_capture_rejects_too_fast_fill(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    body = capture_body(email="fast@example.com")
    body["renderedAt"] = datetime.now(UTC).isoformat()
    resp = await capture(client, body)
    assert resp.status_code == 422


async def test_capture_rate_limited_per_ip(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    for i in range(5):
        resp = await capture(client, capture_body(email=f"rl{i}@example.com"))
        assert resp.status_code == 201, resp.text
    resp = await capture(client, capture_body(email="rl-over@example.com"))
    assert resp.status_code == 429
    assert resp.json()["type"].endswith("rate-limited")


# ---- assignment engine ----


async def test_listing_agent_strategy_assigns_from_listing(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="agent1@a.example.com"
    )
    me = await client.get("/api/v1/users/me", headers=agent)
    agent_id = me.json()["id"]

    listing = await make_listing(client, admin, agentId=agent_id)
    published = await transition(client, admin, listing["id"], "published")
    assert published.status_code == 200

    resp = await capture(
        client,
        capture_body(email="wants-villa@example.com", source="listing_form")
        | {"listingId": listing["id"]},
    )
    assert resp.status_code == 201, resp.text
    lead = await client.get(f"{PORTAL_LEADS}/{resp.json()['id']}", headers=admin)
    assert lead.json()["agentId"] == agent_id
    # Speed-to-lead: the assignment notification reached the agent's inbox.
    assert await mailpit_count("agent1@a.example.com", "lead") >= 1


async def test_listing_agent_strategy_leaves_unassigned_without_listing(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await capture(client, capture_body(email="nolisting@example.com"))
    lead = await client.get(f"{PORTAL_LEADS}/{resp.json()['id']}", headers=admin)
    assert lead.json()["agentId"] is None


async def test_round_robin_distributes_and_respects_caps(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    agent_ids: list[str] = []
    for i in range(3):
        headers = await add_user(
            client,
            create_tenant_user,
            str(tenant["id"]),
            Role.AGENT,
            email=f"rr{i}@a.example.com",
        )
        me = await client.get("/api/v1/users/me", headers=headers)
        agent_ids.append(me.json()["id"])

    rule = await client.put(
        f"{PORTAL_LEADS}/assignment-rule",
        json={"strategy": "round_robin", "config": {}},
        headers=admin,
    )
    assert rule.status_code == 200, rule.text

    for i in range(6):
        resp = await client.post(
            PORTAL_LEADS,
            json={
                "contact": {"email": f"rrlead{i}@example.com"},
                "source": "phone",
            },
            headers=admin,
        )
        assert resp.status_code == 201, resp.text

    items = await portal_leads(client, admin)
    counts: dict[str, int] = {}
    for item in items:
        assert item["agentId"] is not None
        counts[item["agentId"]] = counts.get(item["agentId"], 0) + 1
    assert set(counts) == set(agent_ids)
    assert max(counts.values()) - min(counts.values()) <= 1  # 6 leads / 3 agents → 2 each

    # Cap: pool of one agent already at 2 open leads, cap 1 → unassigned.
    capped = await client.put(
        f"{PORTAL_LEADS}/assignment-rule",
        json={
            "strategy": "round_robin",
            "config": {"agent_pool": [agent_ids[0]], "max_open_leads_per_agent": 1},
        },
        headers=admin,
    )
    assert capped.status_code == 200
    resp = await client.post(
        PORTAL_LEADS,
        json={"contact": {"email": "overcap@example.com"}, "source": "phone"},
        headers=admin,
    )
    assert resp.json()["agentId"] is None


async def test_territory_strategy_rejected_at_write(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.put(
        f"{PORTAL_LEADS}/assignment-rule",
        json={"strategy": "territory", "config": {}},
        headers=admin,
    )
    assert resp.status_code == 409
    # And nothing was configured — GET still reports no rule.
    got = await client.get(f"{PORTAL_LEADS}/assignment-rule", headers=admin)
    assert got.status_code == 404


# ---- pipeline ----


async def test_stage_transition_records_activity_and_lost_requires_reason(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await capture(client, capture_body(email="pipeline@example.com"))
    lead_id = resp.json()["id"]

    moved = await client.post(
        f"{PORTAL_LEADS}/{lead_id}/stage", json={"toStage": "contacted"}, headers=admin
    )
    assert moved.status_code == 200
    assert moved.json()["stage"] == "contacted"

    acts = await client.get(f"{PORTAL_LEADS}/{lead_id}/activities", headers=admin)
    changes = [a for a in acts.json() if a["type"] == "status_change"]
    assert changes and changes[0]["payload"] == {"from": "new", "to": "contacted"}
    assert changes[0]["actorId"] is not None

    lost = await client.post(
        f"{PORTAL_LEADS}/{lead_id}/stage", json={"toStage": "lost"}, headers=admin
    )
    assert lost.status_code == 409  # no reason given

    lost = await client.post(
        f"{PORTAL_LEADS}/{lead_id}/stage",
        json={"toStage": "lost", "lostReason": "went with another agency"},
        headers=admin,
    )
    assert lost.status_code == 200
    assert lost.json()["lostReason"] == "went with another agency"


async def test_stage_advance_stops_drip(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    resp = await capture(client, capture_body(email="dripstop@example.com"))
    lead_id = resp.json()["id"]

    drip = await get_drip_state(app, str(tenant["id"]), lead_id)
    assert drip is not None and drip.stopped_at is None

    moved = await client.post(
        f"{PORTAL_LEADS}/{lead_id}/stage", json={"toStage": "qualified"}, headers=admin
    )
    assert moved.status_code == 200

    drip = await get_drip_state(app, str(tenant["id"]), lead_id)
    assert drip is not None
    assert drip.stopped_at is not None
    assert drip.stopped_reason is not None and drip.stopped_reason.value == "stage_advanced"


# ---- activities & speed-to-lead ----


async def test_activity_sets_first_response_and_stops_drip(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    resp = await capture(client, capture_body(email="responder@example.com"))
    lead_id = resp.json()["id"]
    before = await client.get(f"{PORTAL_LEADS}/{lead_id}", headers=admin)
    assert before.json()["firstResponseAt"] is None

    act = await client.post(
        f"{PORTAL_LEADS}/{lead_id}/activities",
        json={"type": "call", "payload": {"summary": "left a voicemail"}},
        headers=admin,
    )
    assert act.status_code == 201, act.text

    after = await client.get(f"{PORTAL_LEADS}/{lead_id}", headers=admin)
    assert after.json()["firstResponseAt"] is not None

    drip = await get_drip_state(app, str(tenant["id"]), lead_id)
    assert drip is not None
    assert drip.stopped_reason is not None and drip.stopped_reason.value == "replied"


async def test_system_activity_types_cannot_be_logged_directly(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await capture(client, capture_body(email="sysact@example.com"))
    forged = await client.post(
        f"{PORTAL_LEADS}/{resp.json()['id']}/activities",
        json={"type": "status_change", "payload": {"from": "new", "to": "won"}},
        headers=admin,
    )
    assert forged.status_code == 422


# ---- scoring ----


async def test_score_reflects_source_quality_and_engagement(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    warm = await capture(client, capture_body(email="warm@example.com", source="listing_form"))
    cold = await capture(client, capture_body(email="cold@example.com", source="ad"))

    warm_score = (await client.get(f"{PORTAL_LEADS}/{warm.json()['id']}", headers=admin)).json()[
        "score"
    ]
    cold_score = (await client.get(f"{PORTAL_LEADS}/{cold.json()['id']}", headers=admin)).json()[
        "score"
    ]
    assert warm_score > cold_score

    # Engagement recomputes the score upward.
    await client.post(
        f"{PORTAL_LEADS}/{cold.json()['id']}/activities",
        json={"type": "note", "payload": {"text": "called back"}},
        headers=admin,
    )
    bumped = (await client.get(f"{PORTAL_LEADS}/{cold.json()['id']}", headers=admin)).json()[
        "score"
    ]
    assert bumped > cold_score


# ---- drip sweep (Beat task) ----


async def test_drip_sweep_sends_step_and_advances(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    email = f"drip-{uuid.uuid4().hex[:8]}@example.com"
    resp = await capture(client, capture_body(email=email))
    lead_id = resp.json()["id"]

    result = sweep_drips_and_escalations()
    assert result["drips_advanced"] >= 1

    assert await mailpit_count(email, "Thanks") == 1  # day-0 step delivered

    drip = await get_drip_state(app, str(tenant["id"]), lead_id)
    assert drip is not None
    assert drip.step_index == 1
    assert drip.stopped_at is None
    assert drip.next_send_at > datetime.now(UTC)  # day-2 step scheduled ahead

    # An immediate second sweep is a no-op — nothing is due yet.
    again = sweep_drips_and_escalations()
    assert await mailpit_count(email, "Thanks") == 1
    assert isinstance(again["drips_advanced"], int)


async def test_escalation_flags_stale_unassigned_lead_once(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client,
        platform_headers,
        create_tenant_user,
        Role.ADMIN,
        email=f"escadmin-{uuid.uuid4().hex[:8]}@a.example.com",
    )
    resp = await capture(client, capture_body(email="stale-lead@example.com"))
    lead_id = resp.json()["id"]
    await age_lead(app, str(tenant["id"]), lead_id, hours=2)

    result = sweep_drips_and_escalations()
    assert result["leads_escalated"] >= 1

    acts = await client.get(f"{PORTAL_LEADS}/{lead_id}/activities", headers=admin)
    system = [a for a in acts.json() if a["type"] == "system"]
    assert len(system) == 1
    assert system[0]["payload"]["event"] == "escalation_unassigned"

    # Idempotent: a second sweep does not re-escalate the same lead.
    sweep_drips_and_escalations()
    acts = await client.get(f"{PORTAL_LEADS}/{lead_id}/activities", headers=admin)
    assert len([a for a in acts.json() if a["type"] == "system"]) == 1


# ---- scoping & isolation ----


async def test_agents_see_only_their_assigned_leads(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    agent_a = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="scope-a@a.example.com"
    )
    agent_b = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="scope-b@a.example.com"
    )
    a_id = (await client.get("/api/v1/users/me", headers=agent_a)).json()["id"]

    created = await client.post(
        PORTAL_LEADS,
        json={"contact": {"email": "mine@example.com"}, "source": "phone", "agentId": a_id},
        headers=admin,
    )
    lead_id = created.json()["id"]

    # Agent A owns it; agent B gets a 404 (not 403 — no existence oracle).
    assert (await client.get(f"{PORTAL_LEADS}/{lead_id}", headers=agent_a)).status_code == 200
    assert (await client.get(f"{PORTAL_LEADS}/{lead_id}", headers=agent_b)).status_code == 404
    assert (
        await client.post(
            f"{PORTAL_LEADS}/{lead_id}/stage", json={"toStage": "contacted"}, headers=agent_b
        )
    ).status_code == 404
    assert await portal_leads(client, agent_b) == []
    assert len(await portal_leads(client, admin)) == 1


async def test_agent_cannot_reassign_or_edit_contacts(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="reassign@a.example.com"
    )
    agent_id = (await client.get("/api/v1/users/me", headers=agent)).json()["id"]
    admin_id = (await client.get("/api/v1/users/me", headers=admin)).json()["id"]

    created = await client.post(
        PORTAL_LEADS,
        json={"contact": {"email": "owned@example.com"}, "source": "phone", "agentId": agent_id},
        headers=admin,
    )
    lead = created.json()

    # An agent owning the lead still can't hand it to someone else...
    denied = await client.patch(
        f"{PORTAL_LEADS}/{lead['id']}", json={"agentId": admin_id}, headers=agent
    )
    assert denied.status_code == 403
    # ...but a manager can.
    ok = await client.patch(
        f"{PORTAL_LEADS}/{lead['id']}", json={"agentId": admin_id}, headers=admin
    )
    assert ok.status_code == 200 and ok.json()["agentId"] == admin_id

    # Contact writes are manager-gated like contact reads (LEAD_VIEW_ALL) —
    # contact ids leak via LeadOut.contactId, so LEAD_MANAGE alone must not
    # allow blind PATCHes against a colleague's contact.
    denied = await client.patch(
        f"/api/v1/portal/contacts/{lead['contactId']}",
        json={"notes": "hijacked"},
        headers=agent,
    )
    assert denied.status_code == 403
    ok = await client.patch(
        f"/api/v1/portal/contacts/{lead['contactId']}",
        json={"email": "MiXeD@Example.COM"},
        headers=admin,
    )
    assert ok.status_code == 200
    assert ok.json()["email"] == "mixed@example.com"  # normalized on every write path


async def test_assignment_rule_config_is_validated(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    for bad_config in (
        {"agent_pool": ["not-a-uuid"]},
        {"max_open_leads_per_agent": "three"},
        {"max_open_leads_per_agent": 0},
        {"unknown_key": True},
    ):
        resp = await client.put(
            f"{PORTAL_LEADS}/assignment-rule",
            json={"strategy": "round_robin", "config": bad_config},
            headers=admin,
        )
        assert resp.status_code == 422, (bad_config, resp.text)


async def test_capture_rejects_stale_form(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    body = capture_body(email="stale@example.com")
    body["renderedAt"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    resp = await capture(client, body)
    assert resp.status_code == 422


async def test_leads_never_leak_across_tenants(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin_a = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await capture(client, capture_body(email="tenant-a-lead@example.com"))
    lead_id = resp.json()["id"]
    lead = (await client.get(f"{PORTAL_LEADS}/{lead_id}", headers=admin_a)).json()

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

    assert (await client.get(f"{PORTAL_LEADS}/{lead_id}", headers=admin_b)).status_code == 404
    assert (
        await client.get(f"/api/v1/portal/contacts/{lead['contactId']}", headers=admin_b)
    ).status_code == 404
    assert await portal_leads(client, admin_b) == []


async def test_buyer_renter_cannot_access_leads(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    buyer = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.BUYER_RENTER,
        email="buyer@a.example.com",
    )
    assert (await client.get(PORTAL_LEADS, headers=buyer)).status_code == 403


# ---- contact timeline ----


async def test_contact_timeline_merges_leads_and_activities(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    first = await capture(client, capture_body(email="timeline@example.com"))
    second = await capture(client, capture_body(email="timeline@example.com", source="chat"))
    lead_id = second.json()["id"]
    await client.post(
        f"{PORTAL_LEADS}/{lead_id}/activities",
        json={"type": "note", "payload": {"text": "spoke on the phone"}},
        headers=admin,
    )

    contact_id = (await client.get(f"{PORTAL_LEADS}/{first.json()['id']}", headers=admin)).json()[
        "contactId"
    ]
    resp = await client.get(f"/api/v1/portal/contacts/{contact_id}/timeline", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["contact"]["email"] == "timeline@example.com"
    assert len(body["leads"]) == 2
    kinds = [e["kind"] for e in body["entries"]]
    assert kinds.count("lead_created") == 2
    assert kinds.count("activity") == 1
    ats = [e["at"] for e in body["entries"]]
    assert ats == sorted(ats, reverse=True)  # newest first

    # Timeline is a manager view: agents lack LEAD_VIEW_ALL.
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="tl-agent@a.example.com"
    )
    denied = await client.get(f"/api/v1/portal/contacts/{contact_id}/timeline", headers=agent)
    assert denied.status_code == 403
