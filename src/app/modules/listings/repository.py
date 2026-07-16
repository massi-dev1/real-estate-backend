"""DB access for listings. Every method takes ``tenant_id`` (golden rule §5);
ownership scoping (§7.2) is a repository concern too: ``scope_user_id`` narrows
queries to listings an agent owns (assigned or created).
"""

import contextlib
import math
import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import (
    ColumnElement,
    Row,
    Select,
    and_,
    cast,
    false,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.modules.listings.models import Listing, ListingStatus, ListingStatusHistory
from app.modules.listings.schemas import PublicListingFilters, SearchSort

# locale → text-search config. Queries must be parsed with a config from the
# same family the search_vector was built with (migration 0006).
LOCALE_TS_CONFIG: dict[str, str] = {"fr": "french", "en": "english", "ar": "arabic"}

# (featured, sort-key value, id) — the public keyset. The key's Python type
# follows the sort (datetime for newest, Decimal for price/area).
PublicKeyset = tuple[bool, Any, uuid.UUID]


def _sort_key(sort: SearchSort) -> tuple[ColumnElement[Any] | InstrumentedAttribute[Any], bool]:
    """The sort's key expression and whether it descends. ``area_built`` is
    nullable, so area sorts key on ``coalesce(area_built, 0)`` — the cursor
    stores the coalesced value, keeping the keyset total-ordered."""
    match sort:
        case SearchSort.PRICE_ASC:
            return Listing.price, False
        case SearchSort.PRICE_DESC:
            return Listing.price, True
        case SearchSort.AREA_ASC:
            return func.coalesce(Listing.area_built, 0), False
        case SearchSort.AREA_DESC:
            return func.coalesce(Listing.area_built, 0), True
        case _:
            return Listing.published_at, True


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

    def _published_filtered(
        self, tenant_id: uuid.UUID, filters: PublicListingFilters, locale: str
    ) -> Select[tuple[Listing]]:
        """Published rows narrowed by every §8.3 filter (attribute, keyword,
        geo). Shared by the list, map-pin and cluster queries."""
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
        if filters.area_min is not None:
            stmt = stmt.where(Listing.area_built >= filters.area_min)
        if filters.city is not None:
            stmt = stmt.where(
                func.lower(Listing.address["city"].astext) == filters.city.lower()
            )
        if filters.features:
            # JSONB containment (@>) — served by the GIN index on features.
            stmt = stmt.where(Listing.features.contains(filters.features))
        if filters.q is not None:
            config = LOCALE_TS_CONFIG.get(locale, "simple")
            tsquery = func.websearch_to_tsquery(cast(config, REGCONFIG), filters.q)
            stmt = stmt.where(Listing.search_vector.bool_op("@@")(tsquery))
        if filters.bbox is not None:
            min_lon, min_lat, max_lon, max_lat = filters.bbox
            stmt = stmt.where(
                func.ST_Intersects(
                    Listing.location,
                    func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326),
                )
            )
        if filters.near_point is not None:
            lon, lat = filters.near_point
            point = func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
            # Degree-window prefilter the GiST index can serve (longitude
            # degrees shrink with latitude — widen accordingly), then the
            # exact metric cut on geography.
            degrees = filters.near_radius_km / 111.0 / max(math.cos(math.radians(lat)), 0.1)
            stmt = stmt.where(
                Listing.location.op("&&")(func.ST_Expand(point, degrees)),
                func.ST_DWithin(
                    cast(Listing.location, Geography),
                    cast(point, Geography),
                    filters.near_radius_km * 1000,
                ),
            )
        if filters.polygon_wkt is not None:
            # MakeValid + CollectionExtract(3): a self-intersecting drawn
            # polygon degrades to its valid polygon parts instead of erroring.
            polygon = func.ST_CollectionExtract(
                func.ST_MakeValid(func.ST_GeomFromText(filters.polygon_wkt, 4326)), 3
            )
            stmt = stmt.where(func.ST_Intersects(Listing.location, polygon))
        return stmt

    async def list_published(
        self,
        tenant_id: uuid.UUID,
        *,
        filters: PublicListingFilters,
        locale: str,
        sort: SearchSort,
        after: PublicKeyset | None,
        limit: int,
    ) -> list[Listing]:
        """Public keyset page on (featured DESC, sort key, id DESC); returns
        limit+1 rows. Featured leads every sort (§8.3 paid placement)."""
        stmt = self._published_filtered(tenant_id, filters, locale)
        key_col, key_desc = _sort_key(sort)
        if after is not None:
            after_featured, after_key, after_id = after
            key_past = key_col < after_key if key_desc else key_col > after_key
            # "featured strictly past the cursor" — only false-after-true
            # exists in this two-value DESC order.
            featured_past = Listing.featured.is_(False) if after_featured else false()
            stmt = stmt.where(
                or_(
                    featured_past,
                    and_(Listing.featured == after_featured, key_past),
                    and_(
                        Listing.featured == after_featured,
                        key_col == after_key,
                        Listing.id < after_id,
                    ),
                )
            )
        stmt = stmt.order_by(
            Listing.featured.desc(),
            key_col.desc() if key_desc else key_col.asc(),
            Listing.id.desc(),
        ).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    # ---- map (§8.3) ----

    async def count_mappable(
        self, tenant_id: uuid.UUID, *, filters: PublicListingFilters, locale: str
    ) -> int:
        stmt = (
            self._published_filtered(tenant_id, filters, locale)
            .where(Listing.location.is_not(None))
            .with_only_columns(func.count())
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def map_pins(
        self, tenant_id: uuid.UUID, *, filters: PublicListingFilters, locale: str, limit: int
    ) -> list[Row[Any]]:
        stmt = (
            self._published_filtered(tenant_id, filters, locale)
            .where(Listing.location.is_not(None))
            .with_only_columns(
                Listing.id,
                func.ST_Y(Listing.location).label("lat"),
                func.ST_X(Listing.location).label("lng"),
                Listing.price,
                Listing.status,
            )
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).all())

    async def map_clusters(
        self, tenant_id: uuid.UUID, *, filters: PublicListingFilters, locale: str, precision: int
    ) -> list[Row[Any]]:
        """Geohash-bucket clusters: centroid + count per bucket."""
        stmt = (
            self._published_filtered(tenant_id, filters, locale)
            .where(Listing.location.is_not(None))
            .with_only_columns(
                func.avg(func.ST_Y(Listing.location)).label("lat"),
                func.avg(func.ST_X(Listing.location)).label("lng"),
                func.count().label("count"),
            )
            .group_by(func.ST_GeoHash(Listing.location, precision))
        )
        return list((await self.session.execute(stmt)).all())

    # ---- SEO (§8.3) ----

    async def sitemap_rows(self, tenant_id: uuid.UUID, *, limit: int) -> list[Row[Any]]:
        stmt = (
            self._base(tenant_id)
            .where(Listing.status == ListingStatus.PUBLISHED)
            .with_only_columns(Listing.reference_code, Listing.updated_at)
            .order_by(Listing.published_at.desc(), Listing.id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).all())

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
