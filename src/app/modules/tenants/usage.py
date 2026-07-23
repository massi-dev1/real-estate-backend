"""Per-tenant usage counters and write-time quota enforcement (§8.16).

``tenant_usage`` is a global (non-RLS) table, so any session — including a
tenant-scoped one — reads/writes it keyed by ``tenant_id``. Counters are
maintained by the owning services at write-time (a listing create bumps
``listings_count``), so a quota check is an O(1) row read plus a ``FOR UPDATE``
lock (the same count-then-insert race the media pipeline guards, §8.2), never
a cross-module recompute scan.

Boundary usage (no cross-module model imports): listings/agents/media import
``build_usage_boundary(session)`` and call ``check_and_reserve_*`` /
``release_*`` — they never touch ``tenant_usage`` or the plans table directly.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import QuotaExceededError
from app.modules.tenants.models import TenantUsage
from app.modules.tenants.plans import plan_limits, storage_bytes_limit


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    listings_count: int
    agents_count: int
    storage_bytes: int
    emails_sent: int


def _current_period() -> str:
    now = datetime.now(UTC)
    return f"{now.year:04d}-{now.month:02d}"


class UsageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tenant_id: uuid.UUID) -> TenantUsage | None:
        stmt = select(TenantUsage).where(TenantUsage.tenant_id == tenant_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_for_update(self, tenant_id: uuid.UUID) -> TenantUsage:
        """Fetch (creating a zeroed row if absent) with a row lock so a
        concurrent quota check serialises behind it — the same stance as the
        media pipeline's ``FOR UPDATE`` on the listing row."""
        # Ensure the row exists first (idempotent upsert), then lock it.
        await self.session.execute(
            pg_insert(TenantUsage)
            .values(tenant_id=tenant_id)
            .on_conflict_do_nothing(index_elements=["tenant_id"])
        )
        stmt = (
            select(TenantUsage).where(TenantUsage.tenant_id == tenant_id).with_for_update()
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def adjust(
        self,
        tenant_id: uuid.UUID,
        *,
        listings: int = 0,
        agents: int = 0,
        storage: int = 0,
    ) -> None:
        """Atomically bump counters via an upsert. Clamped at zero so a
        double-release can never drive a counter negative (the CHECK
        constraints would otherwise 500). Built from column expressions (not
        raw ``text`` with execution params) so the compiled statement caches
        cleanly."""
        cols = TenantUsage.__table__.c
        stmt = (
            pg_insert(TenantUsage)
            .values(
                tenant_id=tenant_id,
                listings_count=max(listings, 0),
                agents_count=max(agents, 0),
                storage_bytes=max(storage, 0),
            )
            .on_conflict_do_update(
                index_elements=["tenant_id"],
                set_={
                    "listings_count": func.greatest(cols.listings_count + listings, 0),
                    "agents_count": func.greatest(cols.agents_count + agents, 0),
                    "storage_bytes": func.greatest(cols.storage_bytes + storage, 0),
                    "updated_at": func.now(),
                },
            )
        )
        await self.session.execute(stmt)

    async def bump_emails(self, tenant_id: uuid.UUID, count: int, period: str) -> None:
        """Increment the monthly email counter, resetting it when the stored
        period is a previous month."""
        cols = TenantUsage.__table__.c
        stmt = (
            pg_insert(TenantUsage)
            .values(tenant_id=tenant_id, emails_sent=count, emails_period=period)
            .on_conflict_do_update(
                index_elements=["tenant_id"],
                set_={
                    "emails_sent": case(
                        (cols.emails_period == period, cols.emails_sent + count),
                        else_=count,
                    ),
                    "emails_period": period,
                    "updated_at": func.now(),
                },
            )
        )
        await self.session.execute(stmt)


class UsageService:
    """Reads and reserves quota against the tenant's plan (§8.16)."""

    def __init__(self, repo: UsageRepository) -> None:
        self.repo = repo

    async def snapshot(self, tenant_id: uuid.UUID) -> UsageSnapshot:
        row = await self.repo.get(tenant_id)
        if row is None:
            return UsageSnapshot(0, 0, 0, 0)
        emails = row.emails_sent if row.emails_period == _current_period() else 0
        return UsageSnapshot(
            listings_count=row.listings_count,
            agents_count=row.agents_count,
            storage_bytes=row.storage_bytes,
            emails_sent=emails,
        )

    async def reserve_listing(self, tenant_id: uuid.UUID, plan: str) -> None:
        limit = plan_limits(plan).max_listings
        row = await self.repo.get_for_update(tenant_id)
        if limit is not None and row.listings_count >= limit:
            raise QuotaExceededError(
                "Your plan's listing limit has been reached.", limit=limit
            )
        await self.repo.adjust(tenant_id, listings=1)

    async def release_listings(self, tenant_id: uuid.UUID, count: int = 1) -> None:
        await self.repo.adjust(tenant_id, listings=-count)

    async def reserve_agent(self, tenant_id: uuid.UUID, plan: str) -> None:
        limit = plan_limits(plan).max_agents
        row = await self.repo.get_for_update(tenant_id)
        if limit is not None and row.agents_count >= limit:
            raise QuotaExceededError(
                "Your plan's agent limit has been reached.", limit=limit
            )
        await self.repo.adjust(tenant_id, agents=1)

    async def release_agents(self, tenant_id: uuid.UUID, count: int = 1) -> None:
        await self.repo.adjust(tenant_id, agents=-count)

    async def reserve_storage(self, tenant_id: uuid.UUID, plan: str, size_bytes: int) -> None:
        limit = storage_bytes_limit(plan)
        row = await self.repo.get_for_update(tenant_id)
        if limit is not None and row.storage_bytes + size_bytes > limit:
            raise QuotaExceededError(
                "Your plan's storage limit has been reached.", limit_bytes=limit
            )
        await self.repo.adjust(tenant_id, storage=size_bytes)

    async def release_storage(self, tenant_id: uuid.UUID, size_bytes: int) -> None:
        await self.repo.adjust(tenant_id, storage=-size_bytes)

    async def record_emails(self, tenant_id: uuid.UUID, count: int = 1) -> None:
        await self.repo.bump_emails(tenant_id, count, _current_period())


def build_usage_boundary(session: AsyncSession) -> UsageService:
    """Construct a :class:`UsageService` for write-time quota checks in
    listings/agents/media, without pulling ``request`` into their factories."""
    return UsageService(UsageRepository(session))
