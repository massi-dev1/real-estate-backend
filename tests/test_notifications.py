"""Notifications module (§8.12/§13): the unified notify() fan-out (in-app row +
per-channel delivery log + email), the /me surface (list, unread count, mark
read, preferences), preference-driven channel suppression, quiet-hours digest
batching via the Beat sweep, the WebSocket ticket + live-push relay, and the two
migrated real call sites (lead-assignment speed-to-lead, lead escalation).
Plus tenant isolation.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient

from app.core.database import set_tenant_guc
from app.core.permissions import Role
from app.core.tenancy import TenantContext
from app.modules.notifications.models import NotificationType
from app.modules.notifications.service import build_notifications_boundary, user_channel
from app.workers.tasks.leads import sweep_drips_and_escalations
from app.workers.tasks.notifications import send_notification_digests
from tests.helpers import HOST_B
from tests.test_leads import age_lead, capture, capture_body, mailpit_count
from tests.test_listings import add_user, tenant_and_login
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

ME_NOTIFICATIONS = "/api/v1/me/notifications"


async def _notify_direct(
    app: FastAPI,
    tenant: dict[str, Any],
    *,
    user_id: uuid.UUID,
    type: NotificationType,
    payload: dict[str, Any],
    locale: str = "en",
    settings: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Call notify() inside a tenant-scoped request-like transaction, draining
    the post-commit side effects (WS publish + eager send task) exactly as the
    request session would."""
    context = TenantContext(
        id=uuid.UUID(str(tenant["id"])),
        slug=tenant["slug"],
        name=tenant.get("name", tenant["slug"]),
        status="active",
        settings=settings if settings is not None else (tenant.get("settings") or {}),
    )
    async with app.state.session_factory() as session:
        async with session.begin():
            await set_tenant_guc(session, context.id)
            service = build_notifications_boundary(session, app.state.redis)
            note = await service.notify(
                context, user_id=user_id, type=type, payload=payload, locale=locale
            )
            note_id = note.id
        callbacks = session.info.get("post_commit_callbacks", [])
        for callback in callbacks:
            await callback()
    return note_id


async def _user_id(client: AsyncClient, headers: dict[str, str]) -> uuid.UUID:
    resp = await client.get("/api/v1/users/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return uuid.UUID(resp.json()["id"])


# ---- notify() core fan-out ----


async def test_notify_writes_in_app_and_emails(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, agent = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.AGENT, email="notif-agent@a.example.com"
    )
    uid = await _user_id(client, agent)
    before = await mailpit_count("notif-agent@a.example.com", "lead")

    await _notify_direct(
        app,
        tenant,
        user_id=uid,
        type=NotificationType.LEAD_ASSIGNED,
        payload={"leadId": str(uuid.uuid4()), "email": "notif-agent@a.example.com"},
    )

    # In-app row visible on the /me surface.
    resp = await client.get(ME_NOTIFICATIONS, headers=agent)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["type"] == "lead_assigned"
    assert items[0]["readAt"] is None

    # Default channels include email → one delivered via the eager send task.
    after = await mailpit_count("notif-agent@a.example.com", "lead")
    assert after == before + 1


async def test_unread_count_and_mark_read(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, agent = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.AGENT, email="mr@a.example.com"
    )
    uid = await _user_id(client, agent)
    for _ in range(3):
        await _notify_direct(
            app,
            tenant,
            user_id=uid,
            type=NotificationType.LEAD_ASSIGNED,
            payload={"leadId": str(uuid.uuid4())},
        )

    count = await client.get(f"{ME_NOTIFICATIONS}/unread-count", headers=agent)
    assert count.json()["unread"] == 3

    items = (await client.get(ME_NOTIFICATIONS, headers=agent)).json()["items"]
    one = items[0]["id"]
    resp = await client.post(f"{ME_NOTIFICATIONS}/mark-read", json={"ids": [one]}, headers=agent)
    assert resp.status_code == 204
    assert (await client.get(f"{ME_NOTIFICATIONS}/unread-count", headers=agent)).json()[
        "unread"
    ] == 2

    # unreadOnly filter drops the one just read.
    unread = await client.get(ME_NOTIFICATIONS, headers=agent, params={"unreadOnly": True})
    assert len(unread.json()["items"]) == 2

    # Mark all read.
    resp = await client.post(f"{ME_NOTIFICATIONS}/mark-read", json={"all": True}, headers=agent)
    assert resp.status_code == 204
    assert (await client.get(f"{ME_NOTIFICATIONS}/unread-count", headers=agent)).json()[
        "unread"
    ] == 0


# ---- preferences ----


async def test_preferences_default_and_suppress_email(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, agent = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.AGENT, email="pref@a.example.com"
    )
    uid = await _user_id(client, agent)

    prefs = await client.get(f"{ME_NOTIFICATIONS}/preferences", headers=agent)
    assert prefs.status_code == 200
    by_type = {t["type"]: t for t in prefs.json()["types"]}
    la = by_type["lead_assigned"]
    channels = {c["channel"]: c["enabled"] for c in la["channels"]}
    # Type defaults: in_app + email on; sms/whatsapp off.
    assert channels["in_app"] is True and channels["email"] is True
    assert channels["sms"] is False and channels["whatsapp"] is False

    # Turn email off for lead_assigned.
    put = await client.put(
        f"{ME_NOTIFICATIONS}/preferences",
        json={
            "types": [
                {"type": "lead_assigned", "channels": [{"channel": "email", "enabled": False}]}
            ]
        },
        headers=agent,
    )
    assert put.status_code == 200
    la_updated = next(t for t in put.json()["types"] if t["type"] == "lead_assigned")
    updated = {c["channel"]: c["enabled"] for c in la_updated["channels"]}
    assert updated["email"] is False and updated["in_app"] is True

    before = await mailpit_count("pref@a.example.com", "lead")
    await _notify_direct(
        app,
        tenant,
        user_id=uid,
        type=NotificationType.LEAD_ASSIGNED,
        payload={"leadId": str(uuid.uuid4()), "email": "pref@a.example.com"},
    )
    # In-app still written, but no email now.
    assert len((await client.get(ME_NOTIFICATIONS, headers=agent)).json()["items"]) == 1
    assert await mailpit_count("pref@a.example.com", "lead") == before


# ---- quiet hours + digest batching ----


async def test_quiet_hours_do_not_batch_time_sensitive_type(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    # An always-on quiet-hours window so the test time always falls inside it.
    # lead_assigned is NOT digest-eligible (time-sensitive), so it must still
    # send immediately despite quiet hours — never batched into a digest.
    quiet = {"notifications": {"quiet_hours": {"start": "00:00", "end": "23:59"}}}
    tenant, agent = await tenant_and_login(
        client,
        platform_headers,
        create_tenant_user,
        Role.AGENT,
        email="quiet@a.example.com",
        settings=quiet,
    )
    uid = await _user_id(client, agent)
    before = await mailpit_count("quiet@a.example.com", "lead")
    await _notify_direct(
        app,
        tenant,
        user_id=uid,
        type=NotificationType.LEAD_ASSIGNED,
        payload={"leadId": str(uuid.uuid4()), "email": "quiet@a.example.com"},
        settings=quiet,
    )
    # lead_assigned is not digest-eligible → sent now despite quiet hours.
    assert await mailpit_count("quiet@a.example.com", "lead") == before + 1
    # The digest sweep has nothing to batch.
    assert send_notification_digests()["digests_sent"] == 0


async def test_digest_sweep_batches_and_is_idempotent(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """Directly seed digest items (no v1 business type is digest-eligible yet —
    all current notifications are time-sensitive) to prove the batching sweep:
    many parked items collapse into one email per user, and the sent_at stamp
    makes a re-run a no-op."""
    tenant, agent = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.AGENT, email="digest@a.example.com"
    )
    uid = await _user_id(client, agent)
    tid = uuid.UUID(str(tenant["id"]))

    from app.modules.notifications.models import Notification, NotificationChannel
    from app.modules.notifications.repository import NotificationsRepository

    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        repo = NotificationsRepository(session)
        for _ in range(3):
            note = Notification(
                tenant_id=tid,
                user_id=uid,
                type=NotificationType.LEAD_ASSIGNED,
                payload={"leadId": str(uuid.uuid4())},
            )
            repo.add(note)
            await repo.flush()
            repo.enqueue_digest_item(
                tid, user_id=uid, notification_id=note.id, channel=NotificationChannel.EMAIL
            )

    before = await mailpit_count("digest@a.example.com", "new notifications")
    result = send_notification_digests()
    assert result["digests_sent"] == 1
    # Three items → one batched email.
    assert await mailpit_count("digest@a.example.com", "new notifications") == before + 1

    # Idempotent: items are stamped sent, a re-run batches nothing.
    assert send_notification_digests()["digests_sent"] == 0
    assert await mailpit_count("digest@a.example.com", "new notifications") == before + 1


# ---- WebSocket live push ----


async def test_ws_ticket_and_live_push(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, agent = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.AGENT, email="ws@a.example.com"
    )
    uid = await _user_id(client, agent)

    ticket_resp = await client.post(f"{ME_NOTIFICATIONS}/ws-ticket", headers=agent)
    assert ticket_resp.status_code == 200, ticket_resp.text
    ticket = ticket_resp.json()["ticket"]
    assert ticket_resp.json()["expiresIn"] > 0

    # Redeeming the ticket returns the right user for this tenant, single-use.
    from app.modules.notifications.ws import redeem_ws_ticket

    redeemed = await redeem_ws_ticket(
        app.state.redis, ticket, tenant_id=uuid.UUID(str(tenant["id"]))
    )
    assert redeemed == uid
    # Replay fails (GETDEL consumed it).
    assert (
        await redeem_ws_ticket(app.state.redis, ticket, tenant_id=uuid.UUID(str(tenant["id"])))
        is None
    )

    # A notify() to this user publishes to their channel — verify a live
    # subscriber receives it.
    pubsub = app.state.redis.pubsub()
    await pubsub.subscribe(user_channel(uid))
    # Drain the subscribe-confirmation message.
    await pubsub.get_message(timeout=1.0)

    await _notify_direct(
        app,
        tenant,
        user_id=uid,
        type=NotificationType.LEAD_ASSIGNED,
        payload={"leadId": str(uuid.uuid4())},
    )

    message = None
    for _ in range(10):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        if message is not None:
            break
    await pubsub.unsubscribe(user_channel(uid))
    await pubsub.aclose()
    assert message is not None
    data = message["data"]
    text = data.decode() if isinstance(data, bytes | bytearray) else str(data)
    assert "lead_assigned" in text


async def test_ws_ticket_from_other_tenant_rejected(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, agent = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.AGENT, email="wsx@a.example.com"
    )
    ticket = (await client.post(f"{ME_NOTIFICATIONS}/ws-ticket", headers=agent)).json()["ticket"]
    from app.modules.notifications.ws import redeem_ws_ticket

    # A different tenant id can't redeem the ticket.
    assert await redeem_ws_ticket(app.state.redis, ticket, tenant_id=uuid.uuid4()) is None


# ---- migrated real call sites ----


async def test_lead_escalation_notifies_admin_via_notify(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    admin_email = f"escadmin-{uuid.uuid4().hex[:8]}@a.example.com"
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, email=admin_email
    )
    resp = await capture(client, capture_body(email="esc-lead@example.com"))
    lead_id = resp.json()["id"]
    await age_lead(app, str(tenant["id"]), lead_id, hours=2)

    # The tenant-user default locale is French, so the escalation email renders
    # the fr template — "attention" appears in both fr/en subjects.
    before = await mailpit_count(admin_email, "attention")
    result = sweep_drips_and_escalations()
    assert result["leads_escalated"] >= 1

    # The admin got an in-app notification through notify().
    notes = (await client.get(ME_NOTIFICATIONS, headers=admin)).json()["items"]
    escalations = [n for n in notes if n["type"] == "lead_escalated"]
    assert len(escalations) == 1
    assert escalations[0]["payload"]["leadId"] == lead_id
    # And the localized email fired via the delivery task.
    assert await mailpit_count(admin_email, "attention") == before + 1


async def test_lead_assignment_notifies_agent_via_notify(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, email="asadmin@a.example.com"
    )
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="asagent@a.example.com"
    )

    # Round-robin over the sole agent, so a public capture assigns to them and
    # the speed-to-lead notify() trunk fires.
    rule = await client.put(
        "/api/v1/portal/leads/assignment-rule",
        json={"strategy": "round_robin", "config": {}},
        headers=admin,
    )
    assert rule.status_code == 200, rule.text

    resp = await capture(client, capture_body(email="speedlead@example.com", source="phone"))
    assert resp.status_code == 201, resp.text
    lead_id = resp.json()["id"]

    notes = (await client.get(ME_NOTIFICATIONS, headers=agent)).json()["items"]
    assigned = [n for n in notes if n["type"] == "lead_assigned"]
    assert len(assigned) == 1
    assert assigned[0]["payload"]["leadId"] == lead_id


# ---- tenant isolation ----


async def test_notifications_isolated_per_tenant(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant_a, agent_a = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.AGENT, email="iso-a@a.example.com"
    )
    uid_a = await _user_id(client, agent_a)
    await _notify_direct(
        app,
        tenant_a,
        user_id=uid_a,
        type=NotificationType.LEAD_ASSIGNED,
        payload={"leadId": str(uuid.uuid4())},
    )

    # A second tenant (own domain HOST_B); its agent sees nothing of A's.
    tenant_b = await create_tenant(
        client, platform_headers, name="Agency B", slug="agency-b", domain=HOST_B
    )
    agent_b = await add_user(
        client,
        create_tenant_user,
        str(tenant_b["id"]),
        Role.AGENT,
        email="iso-b@b.example.com",
        host=HOST_B,
    )
    resp = await client.get(ME_NOTIFICATIONS, headers=agent_b)
    assert resp.status_code == 200
    assert resp.json()["items"] == []
