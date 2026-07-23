"""DB access for analytics (§8.15). Every method's first arg is ``tenant_id``
(golden rule §5).

Two families of query:

- **Raw events** — a batched insert into the partitioned ``analytics_events``,
  and a per-day aggregate the listing-stats rollup reads. The raw table is only
  ever touched by ingestion and the rollup job (§8.15).
- **Rollups** — idempotent upserts keyed on the natural ``(tenant, dim, day)``
  unique constraints, plus the dashboard reads (only rollup tables leave the
  API).

Partition maintenance (create-ahead / drop-old) is raw DDL on the *default*
(unscoped) connection — a partitioned parent's structure is global, not
tenant-scoped, so those helpers run outside RLS.
"""

import uuid
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from sqlalchemy import Row, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.analytics.models import (
    AnalyticsEvent,
    EventType,
    LeadFunnelDaily,
    ListingStatDaily,
    SourcePerformanceDaily,
)

# Which raw event types roll up into which listing-stat column (§8.15).
_LISTING_COUNTED = {
    EventType.LISTING_VIEW: "views",
    EventType.FAVORITE: "saves",
    EventType.FORM_SUBMIT: "inquiries",
}


class AnalyticsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- raw events ----

    def add_events(self, events: Iterable[AnalyticsEvent]) -> None:
        self.session.add_all(list(events))

    async def listing_event_counts_for_day(
        self, tenant_id: uuid.UUID, day: date
    ) -> list[Row[Any]]:
        """Per-listing counts of the listing-relevant event types on ``day``,
        for the listing-stats rollup. Groups on ``(listing_id, event_type)`` so
        one scan covers views/saves/inquiries."""
        stmt = (
            select(
                AnalyticsEvent.listing_id,
                AnalyticsEvent.event_type,
                func.count().label("n"),
            )
            .where(
                AnalyticsEvent.tenant_id == tenant_id,
                AnalyticsEvent.listing_id.is_not(None),
                AnalyticsEvent.event_type.in_(list(_LISTING_COUNTED)),
                func.date(AnalyticsEvent.created_at) == day,
            )
            .group_by(AnalyticsEvent.listing_id, AnalyticsEvent.event_type)
        )
        return list((await self.session.execute(stmt)).all())

    # ---- rollup upserts (idempotent per (tenant, dim, day)) ----

    async def upsert_listing_stats(
        self,
        tenant_id: uuid.UUID,
        day: date,
        rows: dict[uuid.UUID, dict[str, int]],
    ) -> None:
        """Replace the day's per-listing counts. ``ON CONFLICT`` sets the
        absolute recomputed values (not ``+=``) so a re-run is idempotent."""
        if not rows:
            return
        values = [
            {
                "tenant_id": tenant_id,
                "listing_id": listing_id,
                "day": day,
                "views": counts.get("views", 0),
                "saves": counts.get("saves", 0),
                "inquiries": counts.get("inquiries", 0),
            }
            for listing_id, counts in rows.items()
        ]
        stmt = pg_insert(ListingStatDaily).values(values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_listing_stats_daily_listing_day",
            set_={
                "views": stmt.excluded.views,
                "saves": stmt.excluded.saves,
                "inquiries": stmt.excluded.inquiries,
                "updated_at": func.now(),
            },
        )
        await self.session.execute(stmt)

    async def upsert_lead_funnel(
        self,
        tenant_id: uuid.UUID,
        day: date,
        *,
        leads_created: int,
        leads_won: int,
        leads_lost: int,
    ) -> None:
        stmt = pg_insert(LeadFunnelDaily).values(
            tenant_id=tenant_id,
            day=day,
            leads_created=leads_created,
            leads_won=leads_won,
            leads_lost=leads_lost,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_lead_funnel_daily_day",
            set_={
                "leads_created": stmt.excluded.leads_created,
                "leads_won": stmt.excluded.leads_won,
                "leads_lost": stmt.excluded.leads_lost,
                "updated_at": func.now(),
            },
        )
        await self.session.execute(stmt)

    async def upsert_source_performance(
        self,
        tenant_id: uuid.UUID,
        day: date,
        rows: dict[str, dict[str, int]],
    ) -> None:
        if not rows:
            return
        values = [
            {
                "tenant_id": tenant_id,
                "source": source,
                "day": day,
                "leads_created": counts.get("created", 0),
                "leads_won": counts.get("won", 0),
            }
            for source, counts in rows.items()
        ]
        stmt = pg_insert(SourcePerformanceDaily).values(values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_source_performance_daily_source_day",
            set_={
                "leads_created": stmt.excluded.leads_created,
                "leads_won": stmt.excluded.leads_won,
                "updated_at": func.now(),
            },
        )
        await self.session.execute(stmt)

    # ---- dashboard reads (rollup tables only) ----

    async def traffic_series(
        self, tenant_id: uuid.UUID, *, start: date, end: date
    ) -> list[ListingStatDaily]:
        """Every listing-stat row in the window, ordered by day — the caller
        aggregates the per-day series and totals from these."""
        stmt = (
            select(ListingStatDaily)
            .where(
                ListingStatDaily.tenant_id == tenant_id,
                ListingStatDaily.day >= start,
                ListingStatDaily.day <= end,
            )
            .order_by(ListingStatDaily.day)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def top_listings(
        self, tenant_id: uuid.UUID, *, start: date, end: date, limit: int
    ) -> list[Row[Any]]:
        stmt = (
            select(
                ListingStatDaily.listing_id,
                func.sum(ListingStatDaily.views).label("views"),
                func.sum(ListingStatDaily.saves).label("saves"),
                func.sum(ListingStatDaily.inquiries).label("inquiries"),
            )
            .where(
                ListingStatDaily.tenant_id == tenant_id,
                ListingStatDaily.day >= start,
                ListingStatDaily.day <= end,
            )
            .group_by(ListingStatDaily.listing_id)
            .order_by(func.sum(ListingStatDaily.views).desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).all())

    async def listing_stats_for(
        self,
        tenant_id: uuid.UUID,
        listing_ids: Sequence[uuid.UUID],
        *,
        start: date,
        end: date,
    ) -> list[Row[Any]]:
        """Windowed totals for a specific set of listings — the seller-style
        per-listing report."""
        if not listing_ids:
            return []
        stmt = (
            select(
                ListingStatDaily.listing_id,
                func.sum(ListingStatDaily.views).label("views"),
                func.sum(ListingStatDaily.saves).label("saves"),
                func.sum(ListingStatDaily.inquiries).label("inquiries"),
            )
            .where(
                ListingStatDaily.tenant_id == tenant_id,
                ListingStatDaily.listing_id.in_(list(listing_ids)),
                ListingStatDaily.day >= start,
                ListingStatDaily.day <= end,
            )
            .group_by(ListingStatDaily.listing_id)
        )
        return list((await self.session.execute(stmt)).all())

    async def lead_funnel_series(
        self, tenant_id: uuid.UUID, *, start: date, end: date
    ) -> list[LeadFunnelDaily]:
        stmt = (
            select(LeadFunnelDaily)
            .where(
                LeadFunnelDaily.tenant_id == tenant_id,
                LeadFunnelDaily.day >= start,
                LeadFunnelDaily.day <= end,
            )
            .order_by(LeadFunnelDaily.day)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def source_performance(
        self, tenant_id: uuid.UUID, *, start: date, end: date
    ) -> list[Row[Any]]:
        stmt = (
            select(
                SourcePerformanceDaily.source,
                func.sum(SourcePerformanceDaily.leads_created).label("created"),
                func.sum(SourcePerformanceDaily.leads_won).label("won"),
            )
            .where(
                SourcePerformanceDaily.tenant_id == tenant_id,
                SourcePerformanceDaily.day >= start,
                SourcePerformanceDaily.day <= end,
            )
            .group_by(SourcePerformanceDaily.source)
            .order_by(func.sum(SourcePerformanceDaily.leads_created).desc())
        )
        return list((await self.session.execute(stmt)).all())

    # ---- partition maintenance (global structure, runs unscoped) ----

    async def create_partition(self, name: str, start: date, end: date) -> None:
        """Create a monthly range partition if it doesn't already exist. Idempotent
        via ``IF NOT EXISTS`` (a re-run, or an overlap with the migration's seed
        partitions, is a no-op)."""
        await self.session.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF analytics_events "
                f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
            )
        )

    async def existing_partition_names(self) -> set[str]:
        rows = await self.session.execute(
            text(
                "SELECT inhrelid::regclass::text AS name "
                "FROM pg_inherits "
                "WHERE inhparent = 'analytics_events'::regclass"
            )
        )
        return {r.name for r in rows}

    async def drop_partition(self, name: str) -> None:
        await self.session.execute(text(f"DROP TABLE IF EXISTS {name}"))
