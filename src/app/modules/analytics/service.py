"""Analytics & reporting business logic (§8.15).

Three responsibilities:

- **Ingestion** — validate a batch of anonymous events against the typed
  allowlist and persist them to the partitioned raw table. A consent seam
  (``_consent_allows``) is wired in but currently permissive: §8.17 (cookie
  consent) hasn't shipped yet, so there is nothing to gate against — the hook
  is a TODO the compliance part (Part 23) closes, not a blocker for this part.
- **Dashboards** — read *only* from the daily rollup tables (§8.15), never the
  raw firehose. Traffic/top-listings, lead funnel, source performance, and the
  seller-style per-listing report.
- **Rollup / retention orchestration** — the day-aggregation the nightly Beat
  jobs call, the raw-event prune (drops whole month partitions), and the
  create-ahead partition maintenance. These run per-tenant (rollups) or on the
  unscoped connection (partition DDL, which is global structure).

Rollups pull listing traffic from the raw events and the lead funnel / source
performance from the **leads** module through its boundary accessors — analytics
never imports another module's tables.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated

import structlog
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionDep
from app.core.permissions import AuthenticatedUser
from app.core.tenancy import TenantContext
from app.modules.analytics.models import AnalyticsEvent, EventType
from app.modules.analytics.repository import _LISTING_COUNTED, AnalyticsRepository
from app.modules.analytics.schemas import (
    EventBatchIn,
    LeadFunnelPointOut,
    LeadFunnelSummaryOut,
    ListingPerformanceOut,
    ListingPerformanceReportOut,
    SourcePerformanceOut,
    TopListingOut,
    TrafficPointOut,
    TrafficSummaryOut,
)
from app.modules.compliance.service import ConsentGate, build_consent_gate
from app.modules.leads.service import LeadsService, get_leads_service
from app.modules.listings.service import ListingService, get_listing_service

logger = structlog.get_logger(__name__)

# Raw events older than this are pruned by dropping whole month partitions (§8.15).
RAW_RETENTION_DAYS = 90
# How many future monthly partitions the maintenance job keeps provisioned ahead.
PARTITION_LOOKAHEAD_MONTHS = 3
# A dashboard window can't exceed this — an unbounded range would scan the whole
# rollup history and is never a real dashboard need.
MAX_WINDOW_DAYS = 366
DEFAULT_WINDOW_DAYS = 30
DEFAULT_TOP_LIMIT = 10


def _day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """[start, end) UTC instants bracketing a calendar day — the leads cohort
    aggregation keys on ``created_at`` (a timestamp), so a date needs widening."""
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _add_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _month_partition(year: int, month: int) -> tuple[str, date, date]:
    name = f"analytics_events_{year:04d}_{month:02d}"
    start = date(year, month, 1)
    ny, nm = _add_month(year, month)
    return name, start, date(ny, nm, 1)


@dataclass(frozen=True, slots=True)
class Window:
    start: date
    end: date


class AnalyticsService:
    def __init__(
        self,
        repo: AnalyticsRepository,
        listings: ListingService,
        leads: LeadsService,
        consent: ConsentGate,
    ) -> None:
        self.repo = repo
        self.listings = listings
        self.leads = leads
        self.consent = consent

    # ---- ingestion ----

    async def ingest(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser | None,
        data: EventBatchIn,
    ) -> int:
        """Persist a validated batch of anonymous events, dropping any whose
        (session/user) has not consented to analytics tracking (§8.17 — the gate
        Part 21 left as a TODO). Returns the number *accepted*; the client gets a
        202-shaped ack either way and is never told whether a drop happened.

        Consent is per-session, so a mixed batch is filtered event-by-event; the
        result is cached per session id within one batch to avoid re-querying."""
        user_id = actor.id if actor else None
        allowed_sessions: dict[str | None, bool] = {}
        events: list[AnalyticsEvent] = []
        for event in data.events:
            session_id = event.session_id
            allowed = allowed_sessions.get(session_id)
            if allowed is None:
                allowed = await self.consent.analytics_allowed(
                    tenant, user_id=user_id, session_id=session_id
                )
                allowed_sessions[session_id] = allowed
            if not allowed:
                continue
            events.append(
                AnalyticsEvent(
                    tenant_id=tenant.id,
                    event_type=event.event_type.value,
                    session_id=session_id,
                    user_id=user_id,
                    listing_id=event.listing_id,
                    source=event.source,
                    # The validated, typed payload minus the envelope fields
                    # already stored in their own columns — never raw client JSON.
                    payload=event.model_dump(
                        mode="json",
                        exclude={"event_type", "session_id", "listing_id", "source"},
                    ),
                )
            )
        if not events:
            return 0
        self.repo.add_events(events)
        await self.repo.session.flush()
        return len(events)

    # ---- dashboards (rollups only) ----

    def _resolve_window(self, start: date | None, end: date | None) -> Window:
        resolved_end = end or datetime.now(UTC).date()
        resolved_start = start or (resolved_end - timedelta(days=DEFAULT_WINDOW_DAYS - 1))
        if resolved_start > resolved_end:
            resolved_start = resolved_end
        if (resolved_end - resolved_start).days > MAX_WINDOW_DAYS:
            resolved_start = resolved_end - timedelta(days=MAX_WINDOW_DAYS)
        return Window(start=resolved_start, end=resolved_end)

    async def traffic(
        self, tenant: TenantContext, *, start: date | None, end: date | None
    ) -> TrafficSummaryOut:
        window = self._resolve_window(start, end)
        rows = await self.repo.traffic_series(tenant.id, start=window.start, end=window.end)
        # Collapse per-listing rows into one per-day series (a day has one row
        # per listing in the rollup table).
        by_day: dict[date, dict[str, int]] = {}
        for row in rows:
            acc = by_day.setdefault(row.day, {"views": 0, "saves": 0, "inquiries": 0})
            acc["views"] += row.views
            acc["saves"] += row.saves
            acc["inquiries"] += row.inquiries
        series = [
            TrafficPointOut(day=day, views=v["views"], saves=v["saves"], inquiries=v["inquiries"])
            for day, v in sorted(by_day.items())
        ]
        return TrafficSummaryOut(
            total_views=sum(p.views for p in series),
            total_saves=sum(p.saves for p in series),
            total_inquiries=sum(p.inquiries for p in series),
            series=series,
        )

    async def top_listings(
        self,
        tenant: TenantContext,
        *,
        start: date | None,
        end: date | None,
        limit: int,
    ) -> list[TopListingOut]:
        window = self._resolve_window(start, end)
        rows = await self.repo.top_listings(
            tenant.id, start=window.start, end=window.end, limit=limit
        )
        return [
            TopListingOut(
                listing_id=row.listing_id,
                views=int(row.views or 0),
                saves=int(row.saves or 0),
                inquiries=int(row.inquiries or 0),
            )
            for row in rows
        ]

    async def lead_funnel(
        self, tenant: TenantContext, *, start: date | None, end: date | None
    ) -> LeadFunnelSummaryOut:
        window = self._resolve_window(start, end)
        rows = await self.repo.lead_funnel_series(tenant.id, start=window.start, end=window.end)
        series = [
            LeadFunnelPointOut(
                day=row.day,
                leads_created=row.leads_created,
                leads_won=row.leads_won,
                leads_lost=row.leads_lost,
            )
            for row in rows
        ]
        total_created = sum(p.leads_created for p in series)
        total_won = sum(p.leads_won for p in series)
        return LeadFunnelSummaryOut(
            total_created=total_created,
            total_won=total_won,
            total_lost=sum(p.leads_lost for p in series),
            conversion_rate=round(total_won / total_created, 4) if total_created else 0.0,
            series=series,
        )

    async def source_performance(
        self, tenant: TenantContext, *, start: date | None, end: date | None
    ) -> list[SourcePerformanceOut]:
        window = self._resolve_window(start, end)
        rows = await self.repo.source_performance(tenant.id, start=window.start, end=window.end)
        out: list[SourcePerformanceOut] = []
        for row in rows:
            created = int(row.created or 0)
            won = int(row.won or 0)
            out.append(
                SourcePerformanceOut(
                    source=row.source,
                    leads_created=created,
                    leads_won=won,
                    conversion_rate=round(won / created, 4) if created else 0.0,
                )
            )
        return out

    async def listing_performance(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        *,
        start: date | None,
        end: date | None,
    ) -> ListingPerformanceReportOut:
        """Per-listing views/saves/inquiries over the window, scoped to the
        listings the actor may see (§8.5 visibility, via the listings boundary).
        The seller-style dashboard — sellers don't own inventory as accounts yet,
        so this is portal-scoped; the buyer/seller-account variant is deferred."""
        window = self._resolve_window(start, end)
        listing_ids = await self.listings.scoped_listing_ids(tenant, actor)
        rows = await self.repo.listing_stats_for(
            tenant.id, listing_ids, start=window.start, end=window.end
        )
        return ListingPerformanceReportOut(
            window_start=window.start,
            window_end=window.end,
            listings=[
                ListingPerformanceOut(
                    listing_id=row.listing_id,
                    views=int(row.views or 0),
                    saves=int(row.saves or 0),
                    inquiries=int(row.inquiries or 0),
                )
                for row in rows
            ],
        )

    # ---- rollup orchestration (called by Beat tasks) ----

    async def rollup_day(self, tenant_id: uuid.UUID, day: date) -> None:
        """Aggregate one day of raw events + leads into the three rollup tables
        for one tenant. Idempotent — every write is an upsert on the natural key,
        so a re-run recomputes identical values (§8.15)."""
        await self._rollup_listing_stats(tenant_id, day)
        await self._rollup_lead_funnel(tenant_id, day)
        await self._rollup_source_performance(tenant_id, day)

    async def _rollup_listing_stats(self, tenant_id: uuid.UUID, day: date) -> None:
        rows = await self.repo.listing_event_counts_for_day(tenant_id, day)
        per_listing: dict[uuid.UUID, dict[str, int]] = {}
        for row in rows:
            column = _LISTING_COUNTED.get(EventType(row.event_type))
            if column is None or row.listing_id is None:
                continue
            per_listing.setdefault(row.listing_id, {})[column] = int(row.n)
        await self.repo.upsert_listing_stats(tenant_id, day, per_listing)

    async def _rollup_lead_funnel(self, tenant_id: uuid.UUID, day: date) -> None:
        start, end = _day_bounds_utc(day)
        created, won, lost = await self.leads.funnel_counts_for_day(tenant_id, start, end)
        # Skip an all-zero day so the rollup table doesn't fill with empty rows
        # for tenants with no lead activity.
        if created == 0 and won == 0 and lost == 0:
            return
        await self.repo.upsert_lead_funnel(
            tenant_id, day, leads_created=created, leads_won=won, leads_lost=lost
        )

    async def _rollup_source_performance(self, tenant_id: uuid.UUID, day: date) -> None:
        start, end = _day_bounds_utc(day)
        rows = await self.leads.source_counts_for_day(tenant_id, start, end)
        per_source = {source: {"created": created, "won": won} for source, created, won in rows}
        await self.repo.upsert_source_performance(tenant_id, day, per_source)

    # ---- retention + partition maintenance ----

    async def ensure_partitions(self, now: datetime | None = None) -> list[str]:
        """Create the current + next ``PARTITION_LOOKAHEAD_MONTHS`` monthly
        partitions if missing (create-ahead so an insert never fails for want of
        a partition). Runs on the unscoped connection — partition structure is
        global, not tenant-scoped. Returns the names it created."""
        now = now or datetime.now(UTC)
        existing = await self.repo.existing_partition_names()
        created: list[str] = []
        year, month = now.year, now.month
        for _ in range(PARTITION_LOOKAHEAD_MONTHS + 1):
            name, start, end = _month_partition(year, month)
            if name not in existing:
                await self.repo.create_partition(name, start, end)
                created.append(name)
            year, month = _add_month(year, month)
        return created

    async def prune_raw_events(self, now: datetime | None = None) -> list[str]:
        """Drop whole month partitions entirely older than the retention window
        (§8.15) — far cheaper than a row-by-row DELETE, and the reason to
        partition by month. A partition is droppable once its whole month is
        older than the cutoff. Returns the dropped names."""
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(days=RAW_RETENTION_DAYS)).date()
        existing = await self.repo.existing_partition_names()
        dropped: list[str] = []
        for name in existing:
            month_end = _partition_month_end(name)
            if month_end is not None and month_end <= cutoff:
                await self.repo.drop_partition(name)
                dropped.append(name)
        return dropped


def _partition_month_end(name: str) -> date | None:
    """Parse ``analytics_events_YYYY_MM`` → the first day of the *following*
    month (the partition's exclusive upper bound). A partition is safe to drop
    once that bound is on or before the retention cutoff. Unparseable names
    (should never happen) are left alone."""
    parts = name.rsplit("_", 2)
    if len(parts) != 3:
        return None
    try:
        year, month = int(parts[1]), int(parts[2])
        ny, nm = _add_month(year, month)
        return date(ny, nm, 1)
    except ValueError:
        return None


def get_analytics_service(session: SessionDep) -> AnalyticsService:
    return AnalyticsService(
        AnalyticsRepository(session),
        get_listing_service(session),
        get_leads_service(session),
        build_consent_gate(session),
    )


def build_analytics_service_for_worker(session: AsyncSession) -> AnalyticsService:
    """Worker-side construction (no ``request``). The rollup/prune/partition
    paths never need HTTP context — they read the raw table and leads' boundary
    and write the rollups. The consent gate is unused off the ingestion path."""
    return AnalyticsService(
        AnalyticsRepository(session),
        get_listing_service(session),
        get_leads_service(session),
        build_consent_gate(session),
    )


AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]
