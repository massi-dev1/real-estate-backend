"""HTTP layer for analytics (§8.15).

- ``public_router`` — ``POST /events``: anonymous, batched event ingestion,
  rate-limited like every other public surface (its own bucket). Returns an ack
  count; a consent-gated drop is invisible to the client.
- ``portal_router`` — the dashboards, gated by ``ANALYTICS_VIEW`` (managers).
  Every read is served from the rollup tables, never the raw events. The
  per-listing "seller" report is here too (visibility-scoped via the listings
  boundary); a buyer/seller-account dashboard is deferred until sellers own
  inventory as accounts.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query, status

from app.core.pagination import MAX_PAGE_SIZE
from app.core.permissions import CurrentUserDep, Permission, require
from app.core.rate_limit import rate_limit
from app.core.tenancy import TenantDep
from app.modules.analytics.schemas import (
    EventBatchIn,
    EventBatchOut,
    LeadFunnelSummaryOut,
    ListingPerformanceReportOut,
    SourcePerformanceOut,
    TopListingOut,
    TrafficSummaryOut,
)
from app.modules.analytics.service import DEFAULT_TOP_LIMIT, AnalyticsServiceDep

# ---- public: event ingestion ----

public_router = APIRouter(prefix="/analytics", tags=["analytics:public"])

_ingest_limit = rate_limit(key_prefix="analytics_events", limit=120, window_seconds=60)


@public_router.post(
    "/events",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_ingest_limit)],
)
async def ingest_events(
    data: EventBatchIn,
    tenant: TenantDep,
    service: AnalyticsServiceDep,
) -> EventBatchOut:
    """Anonymous, batched. Accepted-count only; whether consent gated a drop is
    never surfaced to the client."""
    accepted = await service.ingest(tenant, None, data)
    return EventBatchOut(accepted=accepted)


# ---- portal: dashboards (rollups only) ----

portal_router = APIRouter(
    prefix="/portal/analytics",
    tags=["analytics:portal"],
    dependencies=[Depends(require(Permission.ANALYTICS_VIEW))],
)


@portal_router.get("/traffic")
async def traffic(
    tenant: TenantDep,
    service: AnalyticsServiceDep,
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
) -> TrafficSummaryOut:
    return await service.traffic(tenant, start=start, end=end)


@portal_router.get("/top-listings")
async def top_listings(
    tenant: TenantDep,
    service: AnalyticsServiceDep,
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
    limit: int = Query(default=DEFAULT_TOP_LIMIT, ge=1, le=MAX_PAGE_SIZE),
) -> list[TopListingOut]:
    return await service.top_listings(tenant, start=start, end=end, limit=limit)


@portal_router.get("/lead-funnel")
async def lead_funnel(
    tenant: TenantDep,
    service: AnalyticsServiceDep,
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
) -> LeadFunnelSummaryOut:
    return await service.lead_funnel(tenant, start=start, end=end)


@portal_router.get("/sources")
async def source_performance(
    tenant: TenantDep,
    service: AnalyticsServiceDep,
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
) -> list[SourcePerformanceOut]:
    return await service.source_performance(tenant, start=start, end=end)


# The per-listing "seller" report is scoped to the actor's own listings (§8.5),
# so ownership is the authorization — an agent sees only their listings' numbers,
# a manager sees the whole tenant. It therefore sits on its own router gated by
# authentication alone, *not* ANALYTICS_VIEW (which guards the tenant-wide
# aggregate dashboards). Sellers are ordinary portal users for now; a dedicated
# buyer/seller-account dashboard is deferred until sellers own inventory.
listing_report_router = APIRouter(prefix="/portal/analytics", tags=["analytics:portal"])


@listing_report_router.get("/listing-performance")
async def listing_performance(
    tenant: TenantDep,
    service: AnalyticsServiceDep,
    actor: CurrentUserDep,
    start: date | None = Query(default=None),
    end: date | None = Query(default=None),
) -> ListingPerformanceReportOut:
    """Per-listing views/saves/inquiries, scoped to the actor's listings (§8.5)."""
    return await service.listing_performance(tenant, actor, start=start, end=end)
