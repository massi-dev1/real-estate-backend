"""Lead drip + escalation Beat job (§8.4, §12).

Runs every 15 minutes, once per active tenant (RLS is fail-closed — there is
no cross-tenant query, even from a worker). One sweep covers two related,
low-volume, per-tenant checks: advancing due drip-sequence steps, and flagging
leads that have sat unassigned past ``lead_escalation_minutes`` — sharing one
periodic job avoids a second always-on schedule entry for what is otherwise
the same "loop over active tenants" shape as ``flag_stale_listings``.
Escalation only records an admin-notifying activity; it never auto-reassigns
a lead — that stays a human decision.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial

import structlog
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.permissions import Role
from app.core.tenancy import TenantContext
from app.modules.agents.service import build_agents_boundary
from app.modules.leads.models import ActivityType, Lead, LeadActivity, LeadStage
from app.modules.leads.repository import LeadsRepository
from app.modules.leads.service import LeadsService
from app.modules.listings.repository import ListingRepository
from app.modules.listings.service import ListingService
from app.modules.notifications.models import NotificationType
from app.modules.notifications.service import build_notifications_boundary
from app.modules.tenants.models import Tenant, TenantStatus
from app.modules.tenants.usage import build_usage_boundary
from app.modules.users.repository import UserRepository
from app.modules.users.service import UserService
from app.workers.db import run_scoped, run_scoped_many

logger = structlog.get_logger(__name__)


async def _active_tenants(session: AsyncSession) -> list[Tenant]:
    stmt = select(Tenant).where(Tenant.status != TenantStatus.SUSPENDED)
    return list((await session.execute(stmt)).scalars())


def _to_context(tenant: Tenant) -> TenantContext:
    return TenantContext(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        status=tenant.status.value,
        settings=tenant.settings,
    )


async def _advance_drips(session: AsyncSession, tenant: Tenant, now: datetime) -> int:
    context = _to_context(tenant)
    repo = LeadsRepository(session)
    users = UserService(UserRepository(session))
    agents = build_agents_boundary(session)
    service = LeadsService(
        repo,
        users,
        ListingService(ListingRepository(session), users, agents, build_usage_boundary(session)),
        agents,
    )
    due = await repo.list_due_drips(tenant.id, now=now, limit=200)
    for drip in due:
        await service.advance_drip(context, drip)
    return len(due)


async def _escalate_unassigned(session: AsyncSession, tenant: Tenant, now: datetime) -> int:
    settings = get_settings()
    cutoff = now - timedelta(minutes=settings.lead_escalation_minutes)
    # The NOT EXISTS on a prior escalation activity is what makes the sweep
    # idempotent — a lead that stays unassigned is escalated once, not again
    # on every subsequent 15-minute run.
    already_escalated = (
        select(LeadActivity.id)
        .where(
            LeadActivity.lead_id == Lead.id,
            LeadActivity.type == ActivityType.SYSTEM,
            LeadActivity.payload["event"].astext == "escalation_unassigned",
        )
        .exists()
    )
    stmt = select(Lead).where(
        Lead.tenant_id == tenant.id,
        Lead.agent_id.is_(None),
        Lead.stage == LeadStage.NEW,
        Lead.created_at <= cutoff,
        ~already_escalated,
    )
    leads = list((await session.execute(stmt)).scalars())
    if not leads:
        return 0

    user_repo = UserRepository(session)
    admins = await user_repo.list_active_by_role(tenant.id, Role.ADMIN)
    # Escalation now routes through the notifications module (Part 18): each
    # admin gets an in-app row + their enabled external channels (email default),
    # rendered in their locale. notify() registers post-commit side effects,
    # which the worker's scoped transaction drains after commit.
    notifications = build_notifications_boundary(session)
    context = _to_context(tenant)
    for lead in leads:
        session.add(
            LeadActivity(
                tenant_id=tenant.id,
                lead_id=lead.id,
                actor_id=None,
                type=ActivityType.SYSTEM,
                payload={"event": "escalation_unassigned"},
            )
        )
        for admin in admins:
            await notifications.notify(
                context,
                user_id=admin.id,
                type=NotificationType.LEAD_ESCALATED,
                payload={
                    "leadId": str(lead.id),
                    "minutes": settings.lead_escalation_minutes,
                    "email": admin.email,
                },
                locale=admin.locale,
            )
    return len(leads)


@shared_task(name="app.workers.tasks.leads.sweep_drips_and_escalations")
def sweep_drips_and_escalations() -> dict[str, int]:
    """Idempotent: drip advancement moves ``next_send_at`` forward as it
    processes each due row, and escalation carries a NOT EXISTS on the
    prior escalation activity — overlapping or retried runs cannot
    double-send or double-flag."""
    now = datetime.now(UTC)

    async def _list_tenants(session: AsyncSession) -> list[Tenant]:
        return await _active_tenants(session)

    tenants = run_scoped(None, _list_tenants)

    async def _sweep_tenant(session: AsyncSession, *, tenant: Tenant) -> tuple[int, int]:
        drips = await _advance_drips(session, tenant, now)
        escalated = await _escalate_unassigned(session, tenant, now)
        return drips, escalated

    calls: list[tuple[uuid.UUID | None, Callable[[AsyncSession], Awaitable[tuple[int, int]]]]] = [
        (t.id, partial(_sweep_tenant, tenant=t)) for t in tenants
    ]
    results = run_scoped_many(calls)

    total_drips = total_escalated = 0
    for tenant, (drips, escalated) in zip(tenants, results, strict=True):
        total_drips += drips
        total_escalated += escalated
        if drips or escalated:
            logger.info(
                "leads_swept",
                tenant_id=str(tenant.id),
                drips_advanced=drips,
                leads_escalated=escalated,
            )
    return {"drips_advanced": total_drips, "leads_escalated": total_escalated}
