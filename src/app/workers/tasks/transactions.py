"""Deal-milestone reminder Beat job (§8.13, §12).

Runs on a schedule, once per active tenant (RLS is fail-closed — no
cross-tenant query, even from a worker). Every uncompleted milestone with a
due date on or before today, on a still-open deal, notifies its owner (the
milestone owner, or the deal owner as fallback) exactly once — idempotency is
the ``reminder_sent_at`` stamp (same stance as ``appointments.reminder_*_sent_at``
/ ``listings.stale_flagged_at``): a rerun sees no unstamped rows.

Notifications route through Part 18's ``notify()`` — each owner gets an in-app
row + their enabled external channels (email default), rendered in their locale.
``notify()`` registers post-commit side effects, which the worker's scoped
transaction drains after commit.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from functools import partial

import structlog
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantContext
from app.modules.notifications.models import NotificationType
from app.modules.notifications.service import build_notifications_boundary
from app.modules.tenants.models import Tenant, TenantStatus
from app.modules.transactions.repository import TransactionsRepository
from app.modules.users.repository import UserRepository
from app.modules.users.service import UserService
from app.workers.db import run_scoped, run_scoped_many

logger = structlog.get_logger(__name__)

# One tenant's reminder work set is bounded per run — a large agency's overdue
# backlog drains across ticks rather than in one giant transaction.
BATCH_LIMIT = 200


def _to_context(tenant: Tenant) -> TenantContext:
    return TenantContext(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        status=tenant.status.value,
        settings=tenant.settings,
    )


async def _remind_tenant(session: AsyncSession, *, tenant: Tenant, today: date) -> int:
    repo = TransactionsRepository(session)
    due = await repo.due_milestones(tenant.id, on_or_before=today, limit=BATCH_LIMIT)
    if not due:
        return 0

    users = UserService(UserRepository(session))
    # The recipient is the milestone owner, or the deal owner as fallback.
    recipient_ids = {(milestone.owner_user_id or deal.owner_user_id) for milestone, deal in due}
    identities = await users.identities_for(tenant.id, list(recipient_ids))

    notifications = build_notifications_boundary(session)
    context = _to_context(tenant)
    now = datetime.now(UTC)
    sent = 0
    for milestone, deal in due:
        # Stamp regardless of whether a recipient resolves — a milestone whose
        # owner account is gone must never become due again.
        milestone.reminder_sent_at = now
        recipient_id = milestone.owner_user_id or deal.owner_user_id
        identity = identities.get(recipient_id)
        if identity is None:
            continue
        await notifications.notify(
            context,
            user_id=recipient_id,
            type=NotificationType.MILESTONE_DUE,
            payload={
                "milestoneTitle": milestone.title,
                "dealTitle": deal.title,
                "dealId": str(deal.id),
                "milestoneId": str(milestone.id),
                "dueDate": milestone.due_date.isoformat() if milestone.due_date else "",
                "email": identity.email,
            },
            locale=identity.locale,
        )
        sent += 1
    return sent


@shared_task(name="app.workers.tasks.transactions.send_milestone_reminders")
def send_milestone_reminders() -> dict[str, int]:
    """Idempotent: each milestone's ``reminder_sent_at`` is stamped in the same
    transaction that fires its notification, so a rerun sees no due rows."""
    today = datetime.now(UTC).date()

    async def _list_tenants(session: AsyncSession) -> list[Tenant]:
        stmt = select(Tenant).where(Tenant.status != TenantStatus.SUSPENDED)
        return list((await session.execute(stmt)).scalars())

    tenants = run_scoped(None, _list_tenants)

    calls: list[tuple[uuid.UUID | None, Callable[[AsyncSession], Awaitable[int]]]] = [
        (t.id, partial(_remind_tenant, tenant=t, today=today)) for t in tenants
    ]
    results = run_scoped_many(calls)

    total = 0
    for tenant, sent in zip(tenants, results, strict=True):
        total += sent
        if sent:
            logger.info("milestone_reminders_sent", tenant_id=str(tenant.id), count=sent)
    return {"reminders_sent": total}
