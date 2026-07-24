"""Analytics Beat jobs (§8.15, §12): nightly rollups, raw-event pruning, and
partition maintenance. All on the ``analytics`` queue — pure batch work with no
human-facing latency, the same class as ``flag_stale_listings``.

- ``rollup_analytics`` — per active tenant, re-aggregate the previous day (and
  today, so the dashboards stay near-current) from raw events + leads into the
  three rollup tables. Idempotent: every rollup write is an upsert on the natural
  ``(tenant, dim, day)`` key, so a re-run — or overlapping today/yesterday
  passes — recomputes identical values, never double-counts.
- ``prune_analytics_events`` — drop whole month partitions older than the
  retention window (§8.15). Runs unscoped (partition structure is global).
- ``ensure_analytics_partitions`` — create the next few monthly partitions ahead
  of time so an insert never fails for want of a partition. Unscoped.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial

import structlog
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.service import build_analytics_service_for_worker
from app.modules.tenants.models import Tenant, TenantStatus
from app.workers.db import run_ddl, run_scoped, run_scoped_many

logger = structlog.get_logger(__name__)


async def _active_tenant_ids(session: AsyncSession) -> list[uuid.UUID]:
    stmt = select(Tenant.id).where(Tenant.status != TenantStatus.SUSPENDED)
    return list((await session.execute(stmt)).scalars())


async def _rollup_tenant(session: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> None:
    service = build_analytics_service_for_worker(session)
    today = now.date()
    yesterday = today - timedelta(days=1)
    # Re-aggregate yesterday (the day that just fully closed) and today (so a
    # dashboard opened mid-morning already reflects the morning's events).
    await service.rollup_day(tenant_id, yesterday)
    await service.rollup_day(tenant_id, today)


@shared_task(name="app.workers.tasks.analytics.rollup_analytics")
def rollup_analytics() -> int:
    """Aggregate yesterday + today for every active tenant. Returns the tenant
    count processed."""
    now = datetime.now(UTC)

    async def _list_tenants(session: AsyncSession) -> list[uuid.UUID]:
        return await _active_tenant_ids(session)

    tenant_ids = run_scoped(None, _list_tenants)
    calls: list[tuple[uuid.UUID | None, Callable[[AsyncSession], Awaitable[None]]]] = [
        (tid, partial(_rollup_tenant, tenant_id=tid, now=now)) for tid in tenant_ids
    ]
    run_scoped_many(calls)
    logger.info("analytics_rollup_complete", tenants=len(tenant_ids))
    return len(tenant_ids)


@shared_task(name="app.workers.tasks.analytics.prune_analytics_events")
def prune_analytics_events() -> list[str]:
    """Drop month partitions older than the retention window. Unscoped — a
    partitioned parent's structure is global, not tenant-scoped."""

    async def _prune(session: AsyncSession) -> list[str]:
        service = build_analytics_service_for_worker(session)
        return await service.prune_raw_events()

    dropped = run_ddl(_prune)
    if dropped:
        logger.info("analytics_partitions_dropped", partitions=dropped)
    return dropped


@shared_task(name="app.workers.tasks.analytics.ensure_analytics_partitions")
def ensure_analytics_partitions() -> list[str]:
    """Create the current + next months' partitions if missing (create-ahead).
    Unscoped, idempotent (``CREATE TABLE IF NOT EXISTS``)."""

    async def _ensure(session: AsyncSession) -> list[str]:
        service = build_analytics_service_for_worker(session)
        return await service.ensure_partitions()

    created = run_ddl(_ensure)
    if created:
        logger.info("analytics_partitions_created", partitions=created)
    return created
