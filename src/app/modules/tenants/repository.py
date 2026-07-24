"""DB access for tenants. These are *global* platform tables (§4.3): the
tenant here is the aggregate itself, so — uniquely in the codebase — methods
key on the tenant's own primary key instead of taking a ``tenant_id`` scope.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import on_commit
from app.modules.tenants.models import (
    BillingEvent,
    SubscriptionStatus,
    Tenant,
    TenantDomain,
    TenantStatus,
    TenantSubscription,
)


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tenant_id: uuid.UUID) -> Tenant | None:
        stmt = select(Tenant).options(selectinload(Tenant.domains)).where(Tenant.id == tenant_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Tenant | None:
        stmt = select(Tenant).where(Tenant.slug == slug)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_domain(self, domain: str) -> Tenant | None:
        stmt = (
            select(Tenant)
            .join(TenantDomain, TenantDomain.tenant_id == Tenant.id)
            .where(TenantDomain.domain == domain)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def domain_exists(self, domain: str) -> bool:
        stmt = select(TenantDomain.id).where(TenantDomain.domain == domain)
        return (await self.session.execute(stmt)).first() is not None

    async def get_domain(self, tenant_id: uuid.UUID, domain_id: uuid.UUID) -> TenantDomain | None:
        stmt = select(TenantDomain).where(
            TenantDomain.id == domain_id, TenantDomain.tenant_id == tenant_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_page(
        self, *, after: tuple[datetime, uuid.UUID] | None, limit: int
    ) -> list[Tenant]:
        """Keyset page ordered by (created_at, id) descending; fetches limit+1
        rows so the caller can tell whether a next page exists."""
        stmt = (
            select(Tenant)
            .options(selectinload(Tenant.domains))
            .order_by(Tenant.created_at.desc(), Tenant.id.desc())
            .limit(limit + 1)
        )
        if after is not None:
            stmt = stmt.where(tuple_(Tenant.created_at, Tenant.id) < after)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count(self) -> int:
        return (await self.session.execute(select(func.count(Tenant.id)))).scalar_one()

    def add(self, entity: Tenant | TenantDomain) -> None:
        self.session.add(entity)

    async def delete_domain(self, domain: TenantDomain) -> None:
        await self.session.delete(domain)

    async def delete(self, tenant: Tenant) -> None:
        await self.session.delete(tenant)

    # ---- lifecycle / metrics (§8.16) ----

    async def list_trials_expiring(self, *, now: datetime) -> list[Tenant]:
        """Trial tenants whose ``trial_ends_at`` has passed — the trial-expiry
        sweep suspends these unless a subscription has since activated."""
        stmt = (
            select(Tenant)
            .options(selectinload(Tenant.domains))
            .where(
                Tenant.status == TenantStatus.TRIAL,
                Tenant.trial_ends_at.is_not(None),
                Tenant.trial_ends_at <= now,
            )
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_deletions_due(self, *, now: datetime) -> list[Tenant]:
        """Offboarded tenants past their scheduled-deletion instant, not yet
        purged."""
        stmt = select(Tenant).where(
            Tenant.deletion_scheduled_at.is_not(None),
            Tenant.deletion_scheduled_at <= now,
            Tenant.deleted_at.is_(None),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_all(self) -> list[Tenant]:
        stmt = select(Tenant).order_by(Tenant.created_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_by_status(self) -> dict[str, int]:
        stmt = select(Tenant.status, func.count(Tenant.id)).group_by(Tenant.status)
        rows = (await self.session.execute(stmt)).all()
        return {status.value: count for status, count in rows}

    # ---- subscriptions ----

    async def get_subscription(self, tenant_id: uuid.UUID) -> TenantSubscription | None:
        stmt = select(TenantSubscription).where(TenantSubscription.tenant_id == tenant_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_subscription_by_provider_id(
        self, provider: str, provider_subscription_id: str
    ) -> TenantSubscription | None:
        stmt = select(TenantSubscription).where(
            TenantSubscription.provider == provider,
            TenantSubscription.provider_subscription_id == provider_subscription_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_grace_expired_subscriptions(self, *, now: datetime) -> list[TenantSubscription]:
        """Past-due subscriptions whose dunning grace window has closed — the
        dunning sweep suspends their tenants."""
        stmt = select(TenantSubscription).where(
            TenantSubscription.status == SubscriptionStatus.PAST_DUE,
            TenantSubscription.grace_until.is_not(None),
            TenantSubscription.grace_until <= now,
        )
        return list((await self.session.execute(stmt)).scalars().all())

    def add_subscription(self, subscription: TenantSubscription) -> None:
        self.session.add(subscription)

    # ---- billing events (webhook idempotency, §10.9) ----

    async def record_billing_event(
        self, *, provider: str, event_id: str, event_type: str, payload: dict[str, object]
    ) -> bool:
        """Insert the event, returning ``True`` if it is new. A duplicate
        ``(provider, event_id)`` is a no-op returning ``False`` — the idempotency
        guard for a replayed webhook."""
        result = await self.session.execute(
            pg_insert(BillingEvent)
            .values(
                provider=provider,
                event_id=event_id,
                event_type=event_type,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=["provider", "event_id"])
            .returning(BillingEvent.id)
        )
        return result.scalar_one_or_none() is not None

    async def flush(self) -> None:
        await self.session.flush()

    def after_commit(self, callback: Callable[[], Awaitable[None]]) -> None:
        on_commit(self.session, callback)
