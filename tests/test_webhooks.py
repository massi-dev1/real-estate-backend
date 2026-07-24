"""Transactional outbox + outbound webhooks (§12, §8.14, §10.9, Part 31).

Covers:
- the SSRF guard (``core.net``) rejecting private/loopback/link-local/metadata
  targets and accepting a public one;
- outbox durability — a captured lead's speed-to-lead notification is delivered
  by the relay even though the capture never enqueued anything;
- webhook endpoint CRUD + RBAC + secret one-time-reveal;
- signed delivery, HMAC verification, 5xx retry, 4xx no-retry, and the circuit
  breaker opening after N failures;
- tenant isolation (no cross-tenant fetch oracle).
"""

import hashlib
import hmac
import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from app.core.database import set_tenant_guc
from app.core.events import (
    EVENT_LEAD_CREATED,
    OutboxEvent,
    OutboxStatus,
)
from app.core.net import SsrfError, validate_public_url
from app.core.permissions import Role
from app.core.tenancy import TenantContext
from app.modules.webhooks.models import DeliveryStatus, WebhookDelivery, WebhookEndpoint
from app.modules.webhooks.service import (
    CIRCUIT_BREAKER_THRESHOLD,
    DeliveryOutcome,
    build_webhook_service_for_worker,
)
from tests.helpers import HOST_B
from tests.test_leads import (
    CreateTenantUser,
    capture,
    capture_body,
    mailpit_count,
    run_outbox_relay,
)
from tests.test_listings import add_user, make_listing, tenant_and_login, transition
from tests.test_tenants_platform_api import create_tenant

WEBHOOKS = "/api/v1/portal/webhooks"


# ---- SSRF guard (unit, flag off) ----


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata service
        "http://localhost/hook",
        "http://127.0.0.1:8000/hook",
        "http://10.0.0.5/hook",
        "http://192.168.1.10/hook",
        "http://172.16.0.1/hook",
        "http://[::1]/hook",  # IPv6 loopback
        "http://[fe80::1]/hook",  # IPv6 link-local
        "ftp://example.com/hook",  # non-http(s) scheme
        "http://0.0.0.0/hook",  # unspecified
        "not-a-url",
    ],
)
def test_ssrf_guard_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(SsrfError):
        validate_public_url(url, allow_private_hosts=False)


def test_ssrf_guard_accepts_public_target() -> None:
    # A public, resolvable host passes (example.com is IANA-reserved but public).
    validate_public_url("https://example.com/webhooks", allow_private_hosts=False)


def test_ssrf_guard_escape_hatch_allows_private() -> None:
    # The flag lets dev/test deliver to a local mock, but a garbage URL still fails.
    validate_public_url("http://127.0.0.1:9999/hook", allow_private_hosts=True)
    with pytest.raises(SsrfError):
        validate_public_url("nonsense", allow_private_hosts=True)


# ---- outbox durability ----


async def _outbox_rows(app: FastAPI, tenant_id: str) -> list[OutboxEvent]:
    tid = uuid.UUID(tenant_id)
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        rows = (await session.execute(select(OutboxEvent))).scalars().all()
    return list(rows)


async def test_capture_stages_lead_created_outbox_event(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """A capture writes a durable lead.created event in the same transaction —
    it is present and PENDING before any relay runs (§12)."""
    tenant, _admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    resp = await capture(client, capture_body(email="outbox-lead@example.com"))
    assert resp.status_code == 201, resp.text

    rows = await _outbox_rows(app, str(tenant["id"]))
    lead_events = [r for r in rows if r.event_type == EVENT_LEAD_CREATED]
    assert len(lead_events) == 1
    assert lead_events[0].status is OutboxStatus.PENDING


async def test_outbox_delivers_speed_to_lead_even_without_inline_enqueue(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """The reliability guarantee: the capture never enqueued a notification
    (it only staged the outbox event), yet the relay delivers it — so a broker
    hiccup between commit and enqueue can't drop the lead notification."""
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="ob-agent@a.example.com"
    )
    agent_id = (await client.get("/api/v1/users/me", headers=agent)).json()["id"]
    listing = await make_listing(client, admin, agentId=agent_id)
    await transition(client, admin, listing["id"], "published")

    before = await mailpit_count("ob-agent@a.example.com", "prospect")
    resp = await capture(
        client,
        capture_body(email="wants@example.com", source="listing_form")
        | {"listingId": listing["id"]},
    )
    assert resp.status_code == 201, resp.text
    # No email yet — only the outbox event exists.
    assert await mailpit_count("ob-agent@a.example.com", "prospect") == before

    result = run_outbox_relay()
    assert result["delivered"] >= 1
    assert await mailpit_count("ob-agent@a.example.com", "prospect") == before + 1

    # The event is now terminal (delivered) — a re-drain is a no-op.
    rows = await _outbox_rows(app, str(tenant["id"]))
    lead_events = [r for r in rows if r.event_type == EVENT_LEAD_CREATED]
    assert lead_events[0].status is OutboxStatus.DELIVERED
    second = run_outbox_relay()
    assert second["delivered"] == 0
    assert await mailpit_count("ob-agent@a.example.com", "prospect") == before + 1


# ---- webhook endpoint CRUD + RBAC ----


async def _create_endpoint(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    body = {
        "url": "https://hooks.example.com/inbound",
        "events": [EVENT_LEAD_CREATED],
        **overrides,
    }
    resp = await client.post(f"{WEBHOOKS}/endpoints", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def test_endpoint_crud_and_secret_reveal(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    created = await _create_endpoint(client, admin, description="my crm")
    # The secret is revealed exactly once, on creation.
    assert created["secret"]
    endpoint_id = created["id"]

    # ... and never again on read.
    got = await client.get(f"{WEBHOOKS}/endpoints/{endpoint_id}", headers=admin)
    assert got.status_code == 200
    assert "secret" not in got.json()

    listed = await client.get(f"{WEBHOOKS}/endpoints", headers=admin)
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    patched = await client.patch(
        f"{WEBHOOKS}/endpoints/{endpoint_id}",
        json={"isActive": False, "events": ["listing.published"]},
        headers=admin,
    )
    assert patched.status_code == 200
    assert patched.json()["isActive"] is False
    assert patched.json()["events"] == ["listing.published"]

    deleted = await client.delete(f"{WEBHOOKS}/endpoints/{endpoint_id}", headers=admin)
    assert deleted.status_code == 204
    assert (
        await client.get(f"{WEBHOOKS}/endpoints/{endpoint_id}", headers=admin)
    ).status_code == 404


async def test_endpoint_rejects_unknown_event(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.post(
        f"{WEBHOOKS}/endpoints",
        json={"url": "https://hooks.example.com/x", "events": ["not.a.real.event"]},
        headers=admin,
    )
    assert resp.status_code == 422


async def test_endpoint_rejects_internal_url(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """SSRF guard at registration (§10.4). The suite runs with
    allow_private_hosts=true, but a *malformed* / non-http URL is still refused."""
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.post(
        f"{WEBHOOKS}/endpoints",
        json={"url": "ftp://internal/x", "events": [EVENT_LEAD_CREATED]},
        headers=admin,
    )
    assert resp.status_code == 422
    assert resp.json()["type"].endswith("invalid-webhook-url")


async def test_webhook_management_requires_permission(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    # An agent has no WEBHOOK_MANAGE — 403.
    _, agent = await tenant_and_login(client, platform_headers, create_tenant_user, Role.AGENT)
    resp = await client.get(f"{WEBHOOKS}/endpoints", headers=agent)
    assert resp.status_code == 403


# ---- signed delivery + retry + circuit breaker (service-level) ----


def _ctx(tenant: dict[str, Any]) -> TenantContext:
    return TenantContext(
        id=uuid.UUID(tenant["id"]),
        slug=tenant["slug"],
        name=tenant["name"],
        status=tenant["status"],
        settings=tenant["settings"],
    )


async def _seed_endpoint_and_delivery(
    app: FastAPI, tenant_id: str, *, secret: str = "shh"
) -> tuple[uuid.UUID, uuid.UUID]:
    tid = uuid.UUID(tenant_id)
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        endpoint = WebhookEndpoint(
            tenant_id=tid,
            url="https://hooks.example.com/inbound",
            secret=secret,
            events=[EVENT_LEAD_CREATED],
        )
        session.add(endpoint)
        await session.flush()
        delivery = WebhookDelivery(
            tenant_id=tid,
            endpoint_id=endpoint.id,
            event_type=EVENT_LEAD_CREATED,
            payload={"leadId": str(uuid.uuid4())},
        )
        session.add(delivery)
        await session.flush()
        return endpoint.id, delivery.id


async def _deliver_with_status(
    app: FastAPI,
    tenant: dict[str, Any],
    delivery_id: uuid.UUID,
    status_code: int,
    monkeypatch: pytest.MonkeyPatch,
    captured: list[dict[str, Any]] | None = None,
) -> DeliveryOutcome:
    """Run the delivery against a monkeypatched _post returning ``status_code``,
    capturing the signed request when a sink is given."""
    tid = uuid.UUID(tenant["id"])

    async def _fake_post(self: Any, url: str, body: bytes, event_type: str, signature: str) -> int:
        if captured is not None:
            captured.append({"url": url, "body": body, "event": event_type, "signature": signature})
        return status_code

    monkeypatch.setattr(
        "app.modules.webhooks.service.WebhookService._post", _fake_post, raising=True
    )
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        service = build_webhook_service_for_worker(session)
        return await service.deliver(_ctx(tenant), delivery_id)


async def test_delivery_signs_and_succeeds_on_2xx(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    _, delivery_id = await _seed_endpoint_and_delivery(app, str(tenant["id"]), secret="topsecret")

    captured: list[dict[str, Any]] = []
    outcome = await _deliver_with_status(app, tenant, delivery_id, 200, monkeypatch, captured)
    assert outcome.status == "delivered"

    # The signature verifies against the shared secret (Stripe-style t=,v1=).
    sent = captured[0]
    header = sent["signature"]
    parts = dict(item.split("=", 1) for item in header.split(","))
    signed = f"{parts['t']}.".encode() + sent["body"]
    expected = hmac.new(b"topsecret", signed, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(parts["v1"], expected)
    # The body carries the event + data envelope.
    payload = json.loads(sent["body"])
    assert payload["event"] == EVENT_LEAD_CREATED
    assert "leadId" in payload["data"]

    # The delivery row records success.
    tid = uuid.UUID(str(tenant["id"]))
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        row = (
            await session.execute(select(WebhookDelivery).where(WebhookDelivery.id == delivery_id))
        ).scalar_one()
        assert row.status is DeliveryStatus.DELIVERED
        assert row.response_status == 200


async def test_delivery_retries_on_5xx_and_permanent_on_4xx(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)

    # 500 → transient, retry requested, delivery stays pending.
    _, did_5xx = await _seed_endpoint_and_delivery(app, str(tenant["id"]))
    outcome = await _deliver_with_status(app, tenant, did_5xx, 500, monkeypatch)
    assert outcome.retry is True
    assert outcome.status == "failed"

    # 400 → permanent, no retry, delivery failed.
    _, did_4xx = await _seed_endpoint_and_delivery(app, str(tenant["id"]))
    outcome = await _deliver_with_status(app, tenant, did_4xx, 400, monkeypatch)
    assert outcome.retry is False
    assert outcome.status == "failed"
    tid = uuid.UUID(str(tenant["id"]))
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        row = (
            await session.execute(select(WebhookDelivery).where(WebhookDelivery.id == did_4xx))
        ).scalar_one()
        assert row.status is DeliveryStatus.FAILED


async def test_circuit_opens_after_threshold(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After N consecutive failures the breaker opens and delivery stops even
    trying (§8.14) — a subsequent delivery returns 'paused'."""
    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    endpoint_id, _ = await _seed_endpoint_and_delivery(app, str(tenant["id"]))
    tid = uuid.UUID(str(tenant["id"]))

    # Drive the same endpoint to the failure threshold with fresh deliveries.
    last: DeliveryOutcome | None = None
    for _ in range(CIRCUIT_BREAKER_THRESHOLD):
        # Reuse the endpoint but a new delivery each time.
        async with app.state.session_factory() as session, session.begin():
            await set_tenant_guc(session, tid)
            delivery = WebhookDelivery(
                tenant_id=tid,
                endpoint_id=endpoint_id,
                event_type=EVENT_LEAD_CREATED,
                payload={"leadId": str(uuid.uuid4())},
            )
            session.add(delivery)
            await session.flush()
            did = delivery.id
        last = await _deliver_with_status(app, tenant, did, 500, monkeypatch)

    assert last is not None and last.status == "paused"
    # The endpoint's breaker is open.
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        ep = (
            await session.execute(select(WebhookEndpoint).where(WebhookEndpoint.id == endpoint_id))
        ).scalar_one()
        assert ep.circuit_open is True

    # A further delivery on the tripped endpoint short-circuits.
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        delivery = WebhookDelivery(
            tenant_id=tid,
            endpoint_id=endpoint_id,
            event_type=EVENT_LEAD_CREATED,
            payload={"leadId": str(uuid.uuid4())},
        )
        session.add(delivery)
        await session.flush()
        did = delivery.id
    outcome = await _deliver_with_status(app, tenant, did, 200, monkeypatch)
    assert outcome.status == "paused"

    # Re-activating the endpoint clears the breaker (admin recovery path).
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        service = build_webhook_service_for_worker(session)
        from app.modules.webhooks.schemas import WebhookEndpointUpdate

        await service.update_endpoint(
            _ctx(tenant), endpoint_id, WebhookEndpointUpdate(is_active=True)
        )
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        ep = (
            await session.execute(select(WebhookEndpoint).where(WebhookEndpoint.id == endpoint_id))
        ).scalar_one()
        assert ep.circuit_open is False


# ---- end-to-end fan-out through the outbox ----


async def test_lead_created_fans_out_to_webhook(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full path: register an endpoint → capture a lead → relay drains the
    outbox → a delivery row is created and (eager) delivered."""
    sent: list[dict[str, Any]] = []

    async def _fake_post(self: Any, url: str, body: bytes, event_type: str, signature: str) -> int:
        sent.append({"event": event_type, "url": url})
        return 200

    monkeypatch.setattr(
        "app.modules.webhooks.service.WebhookService._post", _fake_post, raising=True
    )

    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await _create_endpoint(client, admin)  # subscribed to lead.created

    resp = await capture(client, capture_body(email="fanout@example.com"))
    assert resp.status_code == 201, resp.text
    # Draining the outbox dispatches the webhook (creating the delivery row +
    # enqueuing the eager delivery task).
    run_outbox_relay()

    assert any(s["event"] == EVENT_LEAD_CREATED for s in sent)
    tid = uuid.UUID(str(tenant["id"]))
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        rows = (await session.execute(select(WebhookDelivery))).scalars().all()
        assert len(list(rows)) == 1
        assert rows[0].status is DeliveryStatus.DELIVERED


# ---- tenant isolation ----


async def test_endpoint_not_visible_across_tenants(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _tenant_a, admin_a = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, email="wa@a.example.com"
    )
    created = await _create_endpoint(client, admin_a)

    tenant_b = await create_tenant(
        client, platform_headers, name="Agency B", slug="agency-b", domain=HOST_B
    )
    admin_b = await add_user(
        client,
        create_tenant_user,
        str(tenant_b["id"]),
        Role.ADMIN,
        email="wb@b.example.com",
        host=HOST_B,
    )
    # Tenant B's admin gets a 404 on tenant A's endpoint — no existence oracle.
    resp = await client.get(f"{WEBHOOKS}/endpoints/{created['id']}", headers=admin_b)
    assert resp.status_code == 404
