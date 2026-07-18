"""Tour reminder Beat job (§8.7, §12).

Runs every 15 minutes, once per active tenant (RLS is fail-closed — no
cross-tenant query, even from a worker). Confirmed appointments get two
email reminders: ~24 hours and ~1 hour before the visit. Idempotency is the
``reminder_24h_sent_at`` / ``reminder_1h_sent_at`` stamps (same stance as
``listings.stale_flagged_at``): a window fires at most once, so overlapping
or retried runs can't double-send. Sending the 1-hour reminder also stamps
the 24-hour column when it's still NULL — a short-notice booking (confirmed
inside the 24h window) gets one reminder, not two back-to-back.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial

import structlog
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.appointments.models import Appointment, AppointmentStatus
from app.modules.leads.repository import LeadsRepository
from app.modules.tenants.models import Tenant, TenantStatus
from app.workers.db import run_scoped, run_scoped_many
from app.workers.tasks.email import send_email

logger = structlog.get_logger(__name__)

REMINDER_WINDOW_24H = timedelta(hours=24)
REMINDER_WINDOW_1H = timedelta(hours=1)


async def _due_reminders(
    session: AsyncSession, tenant_id: uuid.UUID, now: datetime, window: timedelta, *, is_1h: bool
) -> list[Appointment]:
    stamp = Appointment.reminder_1h_sent_at if is_1h else Appointment.reminder_24h_sent_at
    stmt = select(Appointment).where(
        Appointment.tenant_id == tenant_id,
        Appointment.status == AppointmentStatus.CONFIRMED,
        Appointment.start_at > now,
        Appointment.start_at <= now + window,
        stamp.is_(None),
    )
    return list((await session.execute(stmt)).scalars())


async def _remind_tenant(session: AsyncSession, *, tenant: Tenant, now: datetime) -> int:
    # 1h first: it stamps both columns, so the 24h pass below never re-mails
    # a short-notice booking it just covered.
    due_1h = await _due_reminders(session, tenant.id, now, REMINDER_WINDOW_1H, is_1h=True)
    due_24h = [
        a
        for a in await _due_reminders(session, tenant.id, now, REMINDER_WINDOW_24H, is_1h=False)
        if a not in due_1h
    ]
    if not due_1h and not due_24h:
        return 0

    contacts = await LeadsRepository(session).contacts_by_ids(
        tenant.id, {a.contact_id for a in due_1h + due_24h}
    )
    emails = {c.id: c.email for c in contacts if c.email}

    sent = 0
    for appointment, soon in [(a, True) for a in due_1h] + [(a, False) for a in due_24h]:
        if soon:
            appointment.reminder_1h_sent_at = now
        if appointment.reminder_24h_sent_at is None:
            appointment.reminder_24h_sent_at = now
        email = emails.get(appointment.contact_id)
        if not email:
            continue  # stamped anyway — a contact without email never becomes due again
        when = appointment.start_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        subject = (
            "Reminder: your property visit is in one hour"
            if soon
            else "Reminder: your property visit is tomorrow"
        )
        send_email.delay(
            to=email,
            subject=subject,
            text=f"This is a reminder for your property visit on {when}. See you there!",
        )
        sent += 1
    return sent


@shared_task(name="app.workers.tasks.appointments.send_tour_reminders")
def send_tour_reminders() -> dict[str, int]:
    """Idempotent: each window's sent-at stamp is written in the same
    transaction that enqueues the email, so a rerun sees no due rows."""
    now = datetime.now(UTC)

    async def _list_tenants(session: AsyncSession) -> list[Tenant]:
        stmt = select(Tenant).where(Tenant.status != TenantStatus.SUSPENDED)
        return list((await session.execute(stmt)).scalars())

    tenants = run_scoped(None, _list_tenants)

    calls: list[tuple[uuid.UUID | None, Callable[[AsyncSession], Awaitable[int]]]] = [
        (t.id, partial(_remind_tenant, tenant=t, now=now)) for t in tenants
    ]
    results = run_scoped_many(calls)

    total = 0
    for tenant, sent in zip(tenants, results, strict=True):
        total += sent
        if sent:
            logger.info("tour_reminders_sent", tenant_id=str(tenant.id), count=sent)
    return {"reminders_sent": total}
