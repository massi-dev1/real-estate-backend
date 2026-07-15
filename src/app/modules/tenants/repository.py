"""DB access for tenants. These are *global* platform tables (§4.3): the
tenant here is the aggregate itself, so — uniquely in the codebase — methods
key on the tenant's own primary key instead of taking a ``tenant_id`` scope.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import on_commit
from app.modules.tenants.models import Tenant, TenantDomain


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

    async def flush(self) -> None:
        await self.session.flush()

    def after_commit(self, callback: Callable[[], Awaitable[None]]) -> None:
        on_commit(self.session, callback)
