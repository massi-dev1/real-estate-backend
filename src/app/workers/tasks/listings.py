"""Listing-expiry Beat job (§8.1, §12).

Runs nightly, once per tenant (RLS is fail-closed — there is no cross-tenant
query, even from a worker). A published listing is "stale" once it passes its
own ``expires_at``, or — for listings that never set one — once it has been
published longer than ``listing_stale_after_days``. Flagging only sets
``stale_flagged_at`` for agent review; it never changes the listing's status.
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
from app.modules.listings.models import Listing, ListingStatus
from app.modules.tenants.models import Tenant, TenantStatus
from app.workers.db import run_scoped, run_scoped_many

logger = structlog.get_logger(__name__)


async def _active_tenant_ids(session: AsyncSession) -> list[uuid.UUID]:
    stmt = select(Tenant.id).where(Tenant.status != TenantStatus.SUSPENDED)
    return list((await session.execute(stmt)).scalars())


async def _flag_tenant(session: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> int:
    settings = get_settings()
    fallback_cutoff = now - timedelta(days=settings.listing_stale_after_days)
    stmt = select(Listing).where(
        Listing.tenant_id == tenant_id,
        Listing.deleted_at.is_(None),
        Listing.status == ListingStatus.PUBLISHED,
        Listing.stale_flagged_at.is_(None),
        (
            (Listing.expires_at.is_not(None) & (Listing.expires_at <= now))
            | (Listing.expires_at.is_(None) & (Listing.published_at <= fallback_cutoff))
        ),
    )
    listings = list((await session.execute(stmt)).scalars())
    for listing in listings:
        listing.stale_flagged_at = now
    return len(listings)


@shared_task(name="app.workers.tasks.listings.flag_stale_listings")
def flag_stale_listings() -> int:
    """Idempotent: already-flagged listings are excluded, so a retry or an
    overlapping run cannot double-count or re-flag."""
    now = datetime.now(UTC)

    async def _list_tenants(session: AsyncSession) -> list[uuid.UUID]:
        return await _active_tenant_ids(session)

    tenant_ids = run_scoped(None, _list_tenants)

    # One shared engine for the whole batch — SET LOCAL already isolates each
    # tenant's transaction, so a fresh engine per tenant would be pure
    # per-call overhead on a job that may loop over many tenants nightly.
    calls: list[tuple[uuid.UUID | None, Callable[[AsyncSession], Awaitable[int]]]] = [
        (tid, partial(_flag_tenant, tenant_id=tid, now=now)) for tid in tenant_ids
    ]
    counts = run_scoped_many(calls)

    total = 0
    for tenant_id, count in zip(tenant_ids, counts, strict=True):
        total += count
        if count:
            logger.info("listings_flagged_stale", tenant_id=str(tenant_id), count=count)
    return total
