"""Compliance retention & DSR Beat jobs (§8.17, §12).

- ``purge_due_erasures`` — execute data-subject erasure requests whose 30-day
  grace window has closed (§10.12). Per active tenant via ``run_scoped_many``:
  the ``dsr_requests`` table is tenant-RLS, so the due-list query must run inside
  a tenant-scoped transaction (an unscoped ``run_scoped(None, ...)`` would see
  nothing — RLS is fail-closed). Idempotent: an executed request flips to
  ``completed`` and no longer matches the pending filter (same stance as
  ``stale_flagged_at``).
- ``anonymize_stale_lost_leads`` — the 24-month lost-lead retention sweep
  (§8.17), delegating to the compliance service which calls the leads boundary
  (compliance never touches leads' tables). Idempotent: an already-anonymized
  contact is skipped.

Both run on the ``analytics`` queue — pure batch back-office work with no
human-facing latency, same class as the other retention/rollup jobs.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial

import structlog
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantContext
from app.modules.compliance.service import build_compliance_service_for_worker
from app.modules.tenants.models import Tenant, TenantStatus
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


def _list_active_tenants() -> list[tuple[uuid.UUID, TenantContext]]:
    async def _list(session: AsyncSession) -> list[tuple[uuid.UUID, TenantContext]]:
        return [(t.id, _to_context(t)) for t in await _active_tenants(session)]

    return run_scoped(None, _list)


# ---- erasure purge (§10.12) ----


async def _purge_tenant(
    session: AsyncSession, tenant: TenantContext, now: datetime
) -> int:
    service = build_compliance_service_for_worker(session)
    due = await service.repo.list_erasures_due(now=now)
    for dsr in due:
        await service.execute_erasure(tenant, dsr)
    return len(due)


@shared_task(name="app.workers.tasks.compliance.purge_due_erasures")
def purge_due_erasures() -> int:
    """Execute due erasure requests across every active tenant. Idempotent —
    a completed request no longer matches ``list_erasures_due``."""
    now = datetime.now(UTC)
    tenants = _list_active_tenants()
    calls: list[tuple[uuid.UUID | None, Callable[[AsyncSession], Awaitable[int]]]] = [
        (tid, partial(_purge_tenant, tenant=ctx, now=now)) for tid, ctx in tenants
    ]
    counts = run_scoped_many(calls)
    total = 0
    for (tid, _ctx), count in zip(tenants, counts, strict=True):
        total += count
        if count:
            logger.info("dsr_erasures_purged", tenant_id=str(tid), count=count)
    return total


# ---- lost-lead retention (§8.17) ----


async def _anonymize_tenant(
    session: AsyncSession, tenant: TenantContext, now: datetime
) -> int:
    service = build_compliance_service_for_worker(session)
    return await service.anonymize_stale_lost_leads(tenant, now=now)


@shared_task(name="app.workers.tasks.compliance.anonymize_stale_lost_leads")
def anonymize_stale_lost_leads() -> int:
    """Anonymize contacts of LOST leads untouched for 24 months, per active
    tenant. Idempotent — an already-anonymized contact is skipped."""
    now = datetime.now(UTC)
    tenants = _list_active_tenants()
    calls: list[tuple[uuid.UUID | None, Callable[[AsyncSession], Awaitable[int]]]] = [
        (tid, partial(_anonymize_tenant, tenant=ctx, now=now)) for tid, ctx in tenants
    ]
    counts = run_scoped_many(calls)
    total = 0
    for (tid, _ctx), count in zip(tenants, counts, strict=True):
        total += count
        if count:
            logger.info("lost_leads_anonymized", tenant_id=str(tid), count=count)
    return total
