"""DB access for listings. Every method takes ``tenant_id`` (golden rule §5);
ownership scoping (§7.2) is a repository concern too: ``scope_user_id`` narrows
queries to listings an agent owns (assigned or created).
"""

import contextlib
import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.listings.models import Listing, ListingStatus, ListingStatusHistory
from app.modules.listings.schemas import PublicListingFilters


class ListingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _base(
        self, tenant_id: uuid.UUID, *, scope_user_id: uuid.UUID | None = None
    ) -> Select[tuple[Listing]]:
        stmt = select(Listing).where(Listing.tenant_id == tenant_id, Listing.deleted_at.is_(None))
        if scope_user_id is not None:
            stmt = stmt.where(
                or_(Listing.agent_id == scope_user_id, Listing.created_by == scope_user_id)
            )
        return stmt

    async def get(
        self,
        tenant_id: uuid.UUID,
        listing_id: uuid.UUID,
        *,
        scope_user_id: uuid.UUID | None = None,
        for_update: bool = False,
    ) -> Listing | None:
        """``for_update`` locks the row — required by every read-validate-write
        flow (workflow transitions, delete) so concurrent requests re-validate
        against the committed state instead of a stale read."""
        stmt = self._base(tenant_id, scope_user_id=scope_user_id).where(Listing.id == listing_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_published_by_ref_or_id(
        self, tenant_id: uuid.UUID, ref_or_id: str
    ) -> Listing | None:
        """Public detail lookup: reference code first, UUID as fallback."""
        matchers: list[ColumnElement[bool]] = [Listing.reference_code == ref_or_id]
        with contextlib.suppress(ValueError):
            matchers.append(Listing.id == uuid.UUID(ref_or_id))
        stmt = self._base(tenant_id).where(
            Listing.status == ListingStatus.PUBLISHED, or_(*matchers)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_portal(
        self,
        tenant_id: uuid.UUID,
        *,
        scope_user_id: uuid.UUID | None,
        status: ListingStatus | None,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Listing]:
        """Keyset page on (created_at DESC, id DESC); returns limit+1 rows."""
        stmt = self._base(tenant_id, scope_user_id=scope_user_id)
        if status is not None:
            stmt = stmt.where(Listing.status == status)
        if after is not None:
            stmt = stmt.where(
                or_(
                    Listing.created_at < after[0],
                    and_(Listing.created_at == after[0], Listing.id < after[1]),
                )
            )
        stmt = stmt.order_by(Listing.created_at.desc(), Listing.id.desc()).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    async def list_published(
        self,
        tenant_id: uuid.UUID,
        *,
        filters: PublicListingFilters,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Listing]:
        """Public page on (published_at DESC, id DESC); returns limit+1 rows."""
        stmt = self._base(tenant_id).where(Listing.status == ListingStatus.PUBLISHED)
        if filters.purpose is not None:
            stmt = stmt.where(Listing.purpose == filters.purpose)
        if filters.property_type is not None:
            stmt = stmt.where(Listing.property_type == filters.property_type)
        if filters.price_min is not None:
            stmt = stmt.where(Listing.price >= filters.price_min)
        if filters.price_max is not None:
            stmt = stmt.where(Listing.price <= filters.price_max)
        if filters.beds_min is not None:
            stmt = stmt.where(Listing.beds >= filters.beds_min)
        if filters.baths_min is not None:
            stmt = stmt.where(Listing.baths >= filters.baths_min)
        if after is not None:
            stmt = stmt.where(
                or_(
                    Listing.published_at < after[0],
                    and_(Listing.published_at == after[0], Listing.id < after[1]),
                )
            )
        stmt = stmt.order_by(Listing.published_at.desc(), Listing.id.desc()).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    async def count(
        self,
        tenant_id: uuid.UUID,
        *,
        scope_user_id: uuid.UUID | None = None,
        status: ListingStatus | None = None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(Listing)
            .where(Listing.tenant_id == tenant_id, Listing.deleted_at.is_(None))
        )
        if scope_user_id is not None:
            stmt = stmt.where(
                or_(Listing.agent_id == scope_user_id, Listing.created_by == scope_user_id)
            )
        if status is not None:
            stmt = stmt.where(Listing.status == status)
        return (await self.session.execute(stmt)).scalar_one()

    async def next_reference_number(self, tenant_id: uuid.UUID, year: int) -> int:
        """Atomic per-tenant, per-year counter bump (no gap on conflict, no
        duplicate under concurrency)."""
        result = await self.session.execute(
            text(
                "INSERT INTO listing_reference_counters (tenant_id, year, last_value) "
                "VALUES (:tenant_id, :year, 1) "
                "ON CONFLICT (tenant_id, year) DO UPDATE "
                "SET last_value = listing_reference_counters.last_value + 1 "
                "RETURNING last_value"
            ),
            {"tenant_id": str(tenant_id), "year": year},
        )
        return int(result.scalar_one())

    async def history(
        self, tenant_id: uuid.UUID, listing_id: uuid.UUID
    ) -> list[ListingStatusHistory]:
        stmt = (
            select(ListingStatusHistory)
            .where(
                ListingStatusHistory.tenant_id == tenant_id,
                ListingStatusHistory.listing_id == listing_id,
            )
            .order_by(ListingStatusHistory.created_at.desc(), ListingStatusHistory.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    def add(self, obj: Listing | ListingStatusHistory) -> None:
        self.session.add(obj)

    async def flush(self) -> None:
        await self.session.flush()

    async def refresh(self, listing: Listing, fields: list[str] | None = None) -> None:
        await self.session.refresh(listing, fields)
