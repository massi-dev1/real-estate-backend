"""Analytics & reporting (§8.15). All four tables are tenant-owned and
RLS-protected (migration 0020).

- ``AnalyticsEvent`` — the append-only raw firehose. The physical table is
  **range-partitioned by month** on ``created_at`` (native Postgres declarative
  partitioning, set up in the migration's raw DDL — SQLAlchemy models can't
  declare ``PARTITION BY``). The partition key must be part of the primary key,
  so the PK is the composite ``(created_at, id)``; ``id`` is still a UUIDv7 so it
  keeps working as a keyset column. Dashboards **never** read this table — only
  the nightly rollup jobs do (§8.15).
- ``ListingStatDaily`` / ``LeadFunnelDaily`` / ``SourcePerformanceDaily`` — the
  daily rollups the dashboards read from. Each carries a natural unique key
  (``uq_*``) so a re-aggregation of the same day upserts rather than
  double-counts (idempotency, same stance as ``flag_stale_listings``).
"""

import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from uuid_utils.compat import uuid7

from app.core.database import Base, TimestampMixin


class EventType(enum.StrEnum):
    """The tight allowlist of event types a public client may report (§8.15).

    Deliberately small — anonymous clients cannot invent new types, and each
    type carries only a small typed payload (see ``schemas.py``), never
    arbitrary JSONB (an abuse/storage vector)."""

    LISTING_VIEW = "listing_view"
    SEARCH = "search"
    FAVORITE = "favorite"
    FORM_START = "form_start"
    FORM_SUBMIT = "form_submit"
    PAGE_VIEW = "page_view"


class AnalyticsEvent(Base):
    __tablename__ = "analytics_events"

    # Composite PK to satisfy partitioning (created_at is the partition key).
    # Declared to mirror the migration's raw DDL exactly; kept out of the mixins
    # since UUIDPrimaryKeyMixin assumes a single-column id PK.
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), primary_key=True
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    event_type: Mapped[str] = mapped_column(String(40))
    # Anonymous session correlation (a client-generated opaque id) — never PII.
    session_id: Mapped[str | None] = mapped_column(String(64))
    user_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    listing_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    source: Mapped[str | None] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )


class ListingStatDaily(TimestampMixin, Base):
    __tablename__ = "listing_stats_daily"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "listing_id", "day", name="uq_listing_stats_daily_listing_day"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    listing_id: Mapped[uuid.UUID] = mapped_column()
    day: Mapped[date] = mapped_column(Date)
    views: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    saves: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    inquiries: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class LeadFunnelDaily(TimestampMixin, Base):
    __tablename__ = "lead_funnel_daily"
    __table_args__ = (UniqueConstraint("tenant_id", "day", name="uq_lead_funnel_daily_day"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date] = mapped_column(Date)
    leads_created: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    leads_won: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    leads_lost: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class SourcePerformanceDaily(TimestampMixin, Base):
    __tablename__ = "source_performance_daily"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source", "day", name="uq_source_performance_daily_source_day"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(60))
    day: Mapped[date] = mapped_column(Date)
    leads_created: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    leads_won: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
