"""Notification delivery + digest batching (§8.12, §12).

``deliver_notification`` sends one notification over one external channel and
records the attempt in ``notification_sends`` (deliverability is debuggable —
§8.12 point 4). Email goes through the existing SMTP adapter; SMS/WhatsApp have
no provider adapter yet (Parts 8/11 deferrals), so those channels are logged
``skipped`` with a clear "adapter not configured" reason rather than pretending
to send — the send-task *shape* is ready for an adapter to drop in.

``send_notification_digests`` batches parked digest-eligible items (queued
during a user's quiet hours) into one email per user, per active tenant. The
``sent_at`` stamp on each digest item is the idempotency guard (same stance as
``listings.stale_flagged_at`` / tour-reminder stamps): a rerun sees no pending
rows.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any

import structlog
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.integrations.email.service import EmailMessage, SmtpEmailService
from app.modules.notifications.models import (
    NotificationChannel,
    NotificationType,
    SendStatus,
)
from app.modules.notifications.repository import NotificationsRepository
from app.modules.notifications.types import definition_for
from app.modules.tenants.models import Tenant, TenantStatus
from app.modules.users.repository import UserRepository
from app.workers.db import run_scoped, run_scoped_many

logger = structlog.get_logger(__name__)


@shared_task(
    name="app.workers.tasks.notifications.deliver_notification",
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
)
def deliver_notification(
    *,
    tenant_id: str,
    notification_id: str,
    channel: str,
    locale: str,
    payload: dict[str, Any],
) -> None:
    """Deliver one notification over one channel and log the attempt.

    Args are primitives (§12): the task carries the tenant/notification ids +
    the rendered-input payload, never a live ORM object.
    """
    tid = uuid.UUID(tenant_id)
    nid = uuid.UUID(notification_id)
    chan = NotificationChannel(channel)

    async def _deliver(session: AsyncSession) -> None:
        repo = NotificationsRepository(session)
        notification = await repo.get_by_id_for_send(tid, nid)
        if notification is None:
            # Deleted between enqueue and delivery — nothing to send.
            return
        definition = definition_for(notification.type)
        rendered = definition.render(payload, locale)

        if chan is NotificationChannel.EMAIL:
            to = _email_recipient(payload)
            if not to:
                repo.log_send(
                    tid,
                    notification_id=nid,
                    channel=chan,
                    status=SendStatus.SKIPPED,
                    provider_message_id=None,
                    error="no email address on payload",
                    sent_at=None,
                )
                return
            email = SmtpEmailService(get_settings())
            await email.send(
                EmailMessage(to=to, subject=rendered.subject, text=rendered.body)
            )
            repo.log_send(
                tid,
                notification_id=nid,
                channel=chan,
                status=SendStatus.SENT,
                provider_message_id=None,  # SMTP/Mailpit returns no id; provider adapters will.
                error=None,
                sent_at=datetime.now(UTC),
            )
        else:
            # SMS / WhatsApp: no provider adapter configured (Parts 8/11).
            logger.warning(
                "notification_channel_no_adapter",
                channel=chan.value,
                tenant_id=tenant_id,
            )
            repo.log_send(
                tid,
                notification_id=nid,
                channel=chan,
                status=SendStatus.SKIPPED,
                provider_message_id=None,
                error=f"{chan.value} adapter not configured",
                sent_at=None,
            )

    run_scoped(tid, _deliver)


def _email_recipient(payload: dict[str, Any]) -> str | None:
    """The notify() caller puts the recipient's email on the payload under
    ``email`` (it's the delivery target, not derivable from the user id here
    without a users lookup — callers already have the identity)."""
    email = payload.get("email")
    return email if isinstance(email, str) and email else None


# ---- digest batching sweep ----


async def _digest_tenant(session: AsyncSession, *, tenant: Tenant, now: datetime) -> int:
    repo = NotificationsRepository(session)
    pending = await repo.pending_digest_items(tenant.id)
    if not pending:
        return 0

    # Batch per user (rows already ordered by user_id).
    by_user: dict[uuid.UUID, list[Any]] = {}
    item_ids: dict[uuid.UUID, list[uuid.UUID]] = {}
    for item, notification in pending:
        by_user.setdefault(item.user_id, []).append(notification)
        item_ids.setdefault(item.user_id, []).append(item.id)

    users = UserRepository(session)
    email = SmtpEmailService(get_settings())
    sent = 0
    for user_id, notifications in by_user.items():
        user = await users.get(tenant.id, user_id)
        if user is not None and user.email:
            lines = []
            for note in notifications:
                rendered = definition_for(note.type).render(note.payload, user.locale)
                lines.append(f"- {rendered.subject}: {rendered.body}")
            body = "You have new notifications:\n\n" + "\n".join(lines)
            await email.send(
                EmailMessage(
                    to=user.email,
                    subject=f"You have {len(notifications)} new notifications",
                    text=body,
                )
            )
            repo.log_send(
                tenant.id,
                notification_id=None,
                channel=NotificationChannel.EMAIL,
                status=SendStatus.SENT,
                provider_message_id=None,
                error=None,
                sent_at=now,
            )
            sent += 1
        # Stamp the items sent regardless — a user without email never becomes
        # due again (same stance as the tour-reminder sweep).
        await repo.mark_digest_items_sent(tenant.id, item_ids[user_id], now)
    return sent


@shared_task(name="app.workers.tasks.notifications.send_notification_digests")
def send_notification_digests() -> dict[str, int]:
    """Idempotent: each digest item's ``sent_at`` is stamped in the same
    transaction that emails it, so a rerun sees no pending rows."""
    now = datetime.now(UTC)

    async def _list_tenants(session: AsyncSession) -> list[Tenant]:
        stmt = select(Tenant).where(Tenant.status != TenantStatus.SUSPENDED)
        return list((await session.execute(stmt)).scalars())

    tenants = run_scoped(None, _list_tenants)

    calls: list[tuple[uuid.UUID | None, Callable[[AsyncSession], Awaitable[int]]]] = [
        (t.id, partial(_digest_tenant, tenant=t, now=now)) for t in tenants
    ]
    results = run_scoped_many(calls)

    total = 0
    for tenant, sent in zip(tenants, results, strict=True):
        total += sent
        if sent:
            logger.info("notification_digests_sent", tenant_id=str(tenant.id), count=sent)
    return {"digests_sent": total}


# NotificationType re-exported for callers that build payloads by task import.
__all__ = ["NotificationType", "deliver_notification", "send_notification_digests"]
