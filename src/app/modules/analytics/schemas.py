"""Analytics wire schemas (§8.15).

Two concerns:

- **Ingestion** — a public, anonymous, batched surface. The event-type enum is a
  tight allowlist and each type validates against a *small typed payload*, never
  arbitrary JSONB (§8.15 — anonymous clients handing us free-form JSON is an
  abuse/storage vector). Discriminated union on ``eventType`` so a client can't
  smuggle a listing-view's fields into a page-view.
- **Dashboards** — ``*Out`` response models read from the rollup tables. No raw
  events ever leave the API.
"""

import uuid
from datetime import date
from typing import Annotated, Literal

from pydantic import Field

from app.core.schema import InputSchema, OutSchema
from app.modules.analytics.models import EventType

# A single batch cannot report more than this many events — a batched public
# endpoint still needs an upper bound so one request can't dump unbounded rows.
MAX_BATCH_SIZE = 50


# ---- ingestion: per-type typed payloads ----


class _EventBase(InputSchema):
    # A client-generated opaque session id (never PII) so anonymous events can
    # be correlated without a login. Optional — a bare page_view need not carry
    # one. Bounded so it can't bloat the row.
    session_id: str | None = Field(default=None, max_length=64)
    listing_id: uuid.UUID | None = None
    # UTM/source attribution for form + search events; kept short.
    source: str | None = Field(default=None, max_length=60)


class ListingViewEvent(_EventBase):
    event_type: Literal[EventType.LISTING_VIEW]
    listing_id: uuid.UUID  # a listing view without a listing is meaningless


class SearchEvent(_EventBase):
    event_type: Literal[EventType.SEARCH]
    # A denormalized, bounded snapshot of the query — enough to rank popular
    # searches, not the full filter object.
    query: str | None = Field(default=None, max_length=200)
    results_count: int | None = Field(default=None, ge=0)


class FavoriteEvent(_EventBase):
    event_type: Literal[EventType.FAVORITE]
    listing_id: uuid.UUID


class FormStartEvent(_EventBase):
    event_type: Literal[EventType.FORM_START]
    form: str | None = Field(default=None, max_length=60)


class FormSubmitEvent(_EventBase):
    event_type: Literal[EventType.FORM_SUBMIT]
    form: str | None = Field(default=None, max_length=60)


class PageViewEvent(_EventBase):
    event_type: Literal[EventType.PAGE_VIEW]
    path: str | None = Field(default=None, max_length=500)


AnalyticsEventIn = Annotated[
    ListingViewEvent
    | SearchEvent
    | FavoriteEvent
    | FormStartEvent
    | FormSubmitEvent
    | PageViewEvent,
    Field(discriminator="event_type"),
]


class EventBatchIn(InputSchema):
    """A batch of events (§8.15 — the endpoint is batched to keep client
    beacon volume low)."""

    events: list[AnalyticsEventIn] = Field(min_length=1, max_length=MAX_BATCH_SIZE)


class EventBatchOut(OutSchema):
    accepted: int


# ---- dashboards: rollup reads ----


class TrafficPointOut(OutSchema):
    day: date
    views: int
    saves: int
    inquiries: int


class TrafficSummaryOut(OutSchema):
    """Traffic totals + a per-day series over the requested window."""

    total_views: int
    total_saves: int
    total_inquiries: int
    series: list[TrafficPointOut]


class TopListingOut(OutSchema):
    listing_id: uuid.UUID
    views: int
    saves: int
    inquiries: int


class LeadFunnelPointOut(OutSchema):
    day: date
    leads_created: int
    leads_won: int
    leads_lost: int


class LeadFunnelSummaryOut(OutSchema):
    total_created: int
    total_won: int
    total_lost: int
    # Won / created over the window, 0..1, rounded — the headline conversion rate.
    conversion_rate: float
    series: list[LeadFunnelPointOut]


class SourcePerformanceOut(OutSchema):
    source: str
    leads_created: int
    leads_won: int
    conversion_rate: float


class ListingPerformanceOut(OutSchema):
    """The seller-style per-listing dashboard row (§8.15): views / saves /
    inquiries for one listing over the window."""

    listing_id: uuid.UUID
    views: int
    saves: int
    inquiries: int


class ListingPerformanceReportOut(OutSchema):
    window_start: date
    window_end: date
    listings: list[ListingPerformanceOut]
