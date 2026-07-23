"""DB access for listings. Every method takes ``tenant_id`` (golden rule §5);
ownership scoping (§7.2) is a repository concern too: ``scope_user_ids``
narrows queries to listings owned (assigned or created) by any of the given
users — one id for an agent, self + team members for a team lead (§8.5).
"""

import contextlib
import math
import uuid
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal
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

from app.modules.listings.models import (
    Listing,
    ListingPurpose,
    ListingStatus,
    ListingStatusHistory,
    PropertyType,
)
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
        self, tenant_id: uuid.UUID, *, scope_user_ids: Collection[uuid.UUID] | None = None
    ) -> Select[tuple[Listing]]:
        stmt = select(Listing).where(Listing.tenant_id == tenant_id, Listing.deleted_at.is_(None))
        if scope_user_ids is not None:
            ids = list(scope_user_ids)
            stmt = stmt.where(
                or_(Listing.agent_id.in_(ids), Listing.created_by.in_(ids))
            )
        return stmt

    async def get(
        self,
        tenant_id: uuid.UUID,
        listing_id: uuid.UUID,
        *,
        scope_user_ids: Collection[uuid.UUID] | None = None,
        for_update: bool = False,
    ) -> Listing | None:
        """``for_update`` locks the row — required by every read-validate-write
        flow (workflow transitions, delete) so concurrent requests re-validate
        against the committed state instead of a stale read."""
        stmt = self._base(tenant_id, scope_user_ids=scope_user_ids).where(
            Listing.id == listing_id
        )
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
        scope_user_ids: Collection[uuid.UUID] | None,
        status: ListingStatus | None,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Listing]:
        """Keyset page on (created_at DESC, id DESC); returns limit+1 rows."""
        stmt = self._base(tenant_id, scope_user_ids=scope_user_ids)
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
        published_since: datetime | None = None,
    ) -> list[Listing]:
        """Public keyset page on (featured DESC, sort key, id DESC); returns
        limit+1 rows. Featured leads every sort (§8.3 paid placement).
        ``published_since`` is the saved-search digests' watermark (§8.9)."""
        stmt = self._published_filtered(tenant_id, filters, locale)
        if published_since is not None:
            stmt = stmt.where(Listing.published_at > published_since)
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

    def _within_boundary(
        self, tenant_id: uuid.UUID, polygon_wkt: str
    ) -> Select[tuple[Listing]]:
        """Published rows whose point falls inside a MultiPolygon. MakeValid +
        CollectionExtract(3) so a self-intersecting drawn boundary degrades to
        its valid polygon parts instead of erroring (same stance as Part 7's
        ``inPolygon`` filter)."""
        polygon = func.ST_CollectionExtract(
            func.ST_MakeValid(func.ST_GeomFromText(polygon_wkt, 4326)), 3
        )
        return (
            self._base(tenant_id)
            .where(
                Listing.status == ListingStatus.PUBLISHED,
                Listing.location.is_not(None),
                func.ST_Contains(polygon, Listing.location),
            )
        )

    async def list_published_within(
        self,
        tenant_id: uuid.UUID,
        *,
        polygon_wkt: str,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Listing]:
        """Keyset page on (published_at DESC, id DESC) of published listings
        inside a boundary — the neighborhood-guide detail's listing slice."""
        stmt = self._within_boundary(tenant_id, polygon_wkt)
        if after is not None:
            stmt = stmt.where(
                or_(
                    Listing.published_at < after[0],
                    and_(Listing.published_at == after[0], Listing.id < after[1]),
                )
            )
        stmt = stmt.order_by(Listing.published_at.desc(), Listing.id.desc()).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    async def boundary_stats(
        self, tenant_id: uuid.UUID, *, polygon_wkt: str
    ) -> tuple[int, Decimal | None]:
        """(count, median price) of published listings inside a boundary —
        computed in Postgres (``percentile_cont(0.5)``), not app-side. The
        nightly guide-stats job's aggregate."""
        stmt = self._within_boundary(tenant_id, polygon_wkt).with_only_columns(
            func.count(),
            func.percentile_cont(0.5).within_group(Listing.price.asc()),
        )
        row = (await self.session.execute(stmt)).one()
        count, median = row
        return int(count), (Decimal(str(median)) if median is not None else None)

    async def published_by_ids(
        self, tenant_id: uuid.UUID, listing_ids: Collection[uuid.UUID]
    ) -> list[Listing]:
        """Currently-published rows among ``listing_ids`` — the favorites
        dashboard's card join (§8.9); unpublished/deleted ids drop out."""
        if not listing_ids:
            return []
        stmt = self._base(tenant_id).where(
            Listing.status == ListingStatus.PUBLISHED, Listing.id.in_(list(listing_ids))
        )
        return list((await self.session.execute(stmt)).scalars())

    async def published_matches(
        self,
        tenant_id: uuid.UUID,
        listing_id: uuid.UUID,
        *,
        filters: PublicListingFilters,
        locale: str,
    ) -> bool:
        """Does one published listing satisfy a §8.3 filter set? The instant
        saved-search matcher (§8.9) — same filter builder as the list, so the
        two can never disagree on what "matches" means."""
        stmt = (
            self._published_filtered(tenant_id, filters, locale)
            .where(Listing.id == listing_id)
            .with_only_columns(func.count())
        )
        return (await self.session.execute(stmt)).scalar_one() > 0

    async def comps_near(
        self,
        tenant_id: uuid.UUID,
        *,
        lon: float,
        lat: float,
        property_type: PropertyType,
        radius_km: float,
        limit: int,
    ) -> list[tuple[Decimal, Decimal]]:
        """(price, area_built) of comparable sale listings in radius — the
        §8.8 valuation estimator's comp set. Published *and* sold rows count
        (a closed price is the best signal an agency's own data has); rentals
        and rows without an area or a point can't produce a price/m²."""
        point = func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
        degrees = radius_km / 111.0 / max(math.cos(math.radians(lat)), 0.1)
        stmt = (
            select(Listing.price, Listing.area_built)
            .where(
                Listing.tenant_id == tenant_id,
                Listing.deleted_at.is_(None),
                Listing.status.in_((ListingStatus.PUBLISHED, ListingStatus.SOLD)),
                Listing.purpose == ListingPurpose.SALE,
                Listing.property_type == property_type,
                Listing.area_built > 0,
                Listing.location.is_not(None),
                # Same two-stage cut as the public `near` filter: GiST-servable
                # degree window, then the exact metric distance on geography.
                Listing.location.op("&&")(func.ST_Expand(point, degrees)),
                func.ST_DWithin(
                    cast(Listing.location, Geography), cast(point, Geography), radius_km * 1000
                ),
            )
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(price, area) for price, area in rows]

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
        scope_user_ids: Collection[uuid.UUID] | None = None,
        status: ListingStatus | None = None,
    ) -> int:
        stmt = self._base(tenant_id, scope_user_ids=scope_user_ids).with_only_columns(
            func.count()
        )
        if status is not None:
            stmt = stmt.where(Listing.status == status)
        return (await self.session.execute(stmt)).scalar_one()

    async def scoped_listing_ids(
        self, tenant_id: uuid.UUID, *, scope_user_ids: Collection[uuid.UUID] | None
    ) -> list[uuid.UUID]:
        """Ids of every (non-deleted) listing in the actor's scope — ``None``
        scope = tenant-wide. Backs the per-listing analytics report (§8.15)."""
        stmt = self._base(tenant_id, scope_user_ids=scope_user_ids).with_only_columns(
            Listing.id
        )
        return list((await self.session.execute(stmt)).scalars())

    async def list_published_by_agent(
        self, tenant_id: uuid.UUID, agent_user_id: uuid.UUID, *, limit: int
    ) -> list[Listing]:
        """A public agent profile's active listings (§8.5) — assigned only
        (``created_by`` is back-office provenance, not public attribution)."""
        stmt = (
            self._base(tenant_id)
            .where(
                Listing.status == ListingStatus.PUBLISHED, Listing.agent_id == agent_user_id
            )
            .order_by(
                Listing.featured.desc(), Listing.published_at.desc(), Listing.id.desc()
            )
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def counts_by_status_for_agent(
        self, tenant_id: uuid.UUID, agent_user_id: uuid.UUID
    ) -> dict[ListingStatus, int]:
        stmt = (
            select(Listing.status, func.count())
            .where(
                Listing.tenant_id == tenant_id,
                Listing.agent_id == agent_user_id,
                Listing.deleted_at.is_(None),
            )
            .group_by(Listing.status)
        )
        rows = (await self.session.execute(stmt)).all()
        return dict(rows)  # type: ignore[arg-type]

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
