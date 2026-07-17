"""Saved-search alert tasks (§8.9, §12).

- ``match_published_listing`` — enqueued post-commit by the listings workflow
  when a listing enters ``published``: instant alerts.
- ``send_saved_search_digests`` — daily Beat job: daily digests every run,
  weekly digests on Mondays. Idempotent via the per-search ``last_run_at``
  watermark (same stance as ``flag_stale_listings``).

Routed to ``default``: both send human-facing email (the same reasoning that
keeps the leads sweep off the ``analytics`` queue).
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial

import structlog
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.tenancy import TenantContext
from app.modules.agents.service import build_agents_boundary
from app.modules.favorites.repository import FavoritesRepository
from app.modules.favorites.service import FavoritesService
from app.modules.listings.repository import ListingRepository
from app.modules.listings.service import ListingService
from app.modules.tenants.models import Tenant, TenantStatus
from app.modules.users.repository import UserRepository
from app.modules.users.service import UserService
from app.workers.db import run_scoped, run_scoped_many

logger = structlog.get_logger(__name__)


def _to_context(tenant: Tenant) -> TenantContext:
    return TenantContext(
        id=tenant.id, slug=tenant.slug, name=tenant.name, status=tenant.status.value,
        settings=tenant.settings,
    )


def _build_service(session: AsyncSession) -> FavoritesService:
    """Alert-matching construction: no leads/redis (those serve the signup
    flows on the request path); settings only signs unsubscribe tokens."""
    users = UserService(UserRepository(session))
    listings = ListingService(
        ListingRepository(session), users, build_agents_boundary(session)
    )
    return FavoritesService(
        FavoritesRepository(session), listings, users, settings=get_settings()
    )


@shared_task(name="app.workers.tasks.favorites.match_published_listing")
def match_published_listing(tenant_id: str, listing_id: str) -> int:
    """At-least-once: a broker redelivery can re-email a matched search — an
    accepted rarity for an event-shaped notification (no watermark exists that
    could dedupe instant matches without serializing every publish)."""
    tid = uuid.UUID(tenant_id)

    async def _load_tenant(session: AsyncSession) -> Tenant | None:
        return (
            await session.execute(select(Tenant).where(Tenant.id == tid))
        ).scalar_one_or_none()

    tenant = run_scoped(None, _load_tenant)
    if tenant is None or tenant.status == TenantStatus.SUSPENDED:
        return 0

    async def _match(session: AsyncSession) -> int:
        return await _build_service(session).match_published_listing(
            _to_context(tenant), uuid.UUID(listing_id)
        )

    sent = run_scoped(tid, _match)
    if sent:
        logger.info(
            "instant_alerts_sent", tenant_id=tenant_id, listing_id=listing_id, count=sent
        )
    return sent


@shared_task(name="app.workers.tasks.favorites.send_saved_search_digests")
def send_saved_search_digests() -> dict[str, int]:
    now = datetime.now(UTC)

    async def _list_tenants(session: AsyncSession) -> list[Tenant]:
        stmt = select(Tenant).where(Tenant.status != TenantStatus.SUSPENDED)
        return list((await session.execute(stmt)).scalars())

    tenants = run_scoped(None, _list_tenants)

    async def _digest_tenant(session: AsyncSession, *, tenant: Tenant) -> int:
        return await _build_service(session).run_digests(_to_context(tenant), now)

    calls: list[tuple[uuid.UUID | None, Callable[[AsyncSession], Awaitable[int]]]] = [
        (t.id, partial(_digest_tenant, tenant=t)) for t in tenants
    ]
    results = run_scoped_many(calls)

    total = 0
    for tenant, sent in zip(tenants, results, strict=True):
        total += sent
        if sent:
            logger.info("digests_sent", tenant_id=str(tenant.id), count=sent)
    return {"digests_sent": total}
