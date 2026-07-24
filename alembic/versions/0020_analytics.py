"""analytics & reporting (§8.15): partitioned raw events + daily rollups

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-23

Four tenant-owned, RLS-protected tables:

- ``analytics_events`` — the append-only raw firehose, **monthly range
  partitions** on ``created_at`` (native Postgres declarative partitioning).
  The partition key must be part of every unique/primary key, so the PK is the
  composite ``(created_at, id)`` (``id`` is still a UUIDv7, time-ordered, so it
  keeps working as a keyset column). RLS is enabled on the *parent* — Postgres
  propagates the policy to every partition automatically (PG11+), so a partition
  never needs its own policy. This migration creates the parent plus the
  previous/current/next month partitions so inserts don't fail before the
  partition-maintenance Beat job (``ensure_analytics_partitions``) runs; that
  job creates future partitions ahead of time and the prune job drops whole old
  partitions (the whole point of partitioning by month).
- ``listing_stats_daily`` / ``lead_funnel_daily`` / ``source_performance_daily``
  — the nightly rollups the dashboards read from (never the raw events). Each
  has a natural unique key ``(tenant_id, <dimension>, day)`` so re-aggregating a
  day upserts instead of double-counting (same idempotency stance as
  ``flag_stale_listings``).

Alembic autogenerate can't emit ``PARTITION BY``/``CREATE TABLE ... PARTITION
OF``, so the ``analytics_events`` parent + partitions are hand-written raw DDL;
the three rollup tables use the normal ``op.create_table`` helpers.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENTS = "analytics_events"
_LISTING_STATS = "listing_stats_daily"
_LEAD_FUNNEL = "lead_funnel_daily"
_SOURCE_PERF = "source_performance_daily"


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    """First day of the given month and of the next — the ``FROM``/``TO`` of a
    monthly range partition."""
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year + 1:04d}-01-01" if month == 12 else f"{year:04d}-{month + 1:02d}-01"
    return start, end


def _partition_name(year: int, month: int) -> str:
    return f"{_EVENTS}_{year:04d}_{month:02d}"


def _create_partition(year: int, month: int) -> None:
    start, end = _month_bounds(year, month)
    name = _partition_name(year, month)
    op.execute(
        f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {_EVENTS} "
        f"FOR VALUES FROM ('{start}') TO ('{end}')"
    )


def _adjacent_months(now: datetime) -> list[tuple[int, int]]:
    """(prev, current, next) month as (year, month) tuples."""
    year, month = now.year, now.month
    prev = (year - 1, 12) if month == 1 else (year, month - 1)
    nxt = (year + 1, 1) if month == 12 else (year, month + 1)
    return [prev, (year, month), nxt]


def upgrade() -> None:
    # --- analytics_events (partitioned parent, raw DDL) ---
    # Composite PK (created_at, id): the partition key (created_at) must be part
    # of the PK for a partitioned table. id stays a UUIDv7 for keyset reads.
    op.execute(
        f"""
        CREATE TABLE {_EVENTS} (
            id uuid NOT NULL,
            tenant_id uuid NOT NULL,
            event_type varchar(40) NOT NULL,
            session_id varchar(64),
            user_id uuid,
            listing_id uuid,
            source varchar(60),
            payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_{_EVENTS} PRIMARY KEY (created_at, id),
            CONSTRAINT fk_{_EVENTS}_tenant_id_tenants
                FOREIGN KEY (tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
        ) PARTITION BY RANGE (created_at)
        """
    )
    # Per-tenant time-ordered reads (the rollup scans key on this).
    op.execute(f"CREATE INDEX ix_{_EVENTS}_tenant_created ON {_EVENTS} (tenant_id, created_at)")
    op.execute(
        f"CREATE INDEX ix_{_EVENTS}_tenant_type_created "
        f"ON {_EVENTS} (tenant_id, event_type, created_at)"
    )

    for year, month in _adjacent_months(datetime.now(UTC)):
        _create_partition(year, month)

    # RLS on the parent propagates to all partitions (PG11+).
    for stmt in enable_tenant_rls_sql(_EVENTS):
        op.execute(stmt)

    # --- listing_stats_daily ---
    op.create_table(
        _LISTING_STATS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("saves", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("inquiries", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_listing_stats_daily_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_listing_stats_daily")),
        sa.UniqueConstraint(
            "tenant_id", "listing_id", "day", name="uq_listing_stats_daily_listing_day"
        ),
    )
    op.create_index(
        op.f("ix_listing_stats_daily_tenant_id"), _LISTING_STATS, ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_listing_stats_daily_tenant_day", _LISTING_STATS, ["tenant_id", "day"], unique=False
    )
    for stmt in enable_tenant_rls_sql(_LISTING_STATS):
        op.execute(stmt)

    # --- lead_funnel_daily ---
    op.create_table(
        _LEAD_FUNNEL,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("leads_created", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("leads_won", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("leads_lost", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_lead_funnel_daily_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lead_funnel_daily")),
        sa.UniqueConstraint("tenant_id", "day", name="uq_lead_funnel_daily_day"),
    )
    op.create_index(
        op.f("ix_lead_funnel_daily_tenant_id"), _LEAD_FUNNEL, ["tenant_id"], unique=False
    )
    for stmt in enable_tenant_rls_sql(_LEAD_FUNNEL):
        op.execute(stmt)

    # --- source_performance_daily ---
    op.create_table(
        _SOURCE_PERF,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=60), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("leads_created", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("leads_won", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_source_performance_daily_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_performance_daily")),
        sa.UniqueConstraint(
            "tenant_id", "source", "day", name="uq_source_performance_daily_source_day"
        ),
    )
    op.create_index(
        op.f("ix_source_performance_daily_tenant_id"), _SOURCE_PERF, ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_source_performance_daily_tenant_day",
        _SOURCE_PERF,
        ["tenant_id", "day"],
        unique=False,
    )
    for stmt in enable_tenant_rls_sql(_SOURCE_PERF):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in disable_tenant_rls_sql(_SOURCE_PERF):
        op.execute(stmt)
    op.drop_index("ix_source_performance_daily_tenant_day", table_name=_SOURCE_PERF)
    op.drop_index(op.f("ix_source_performance_daily_tenant_id"), table_name=_SOURCE_PERF)
    op.drop_table(_SOURCE_PERF)

    for stmt in disable_tenant_rls_sql(_LEAD_FUNNEL):
        op.execute(stmt)
    op.drop_index(op.f("ix_lead_funnel_daily_tenant_id"), table_name=_LEAD_FUNNEL)
    op.drop_table(_LEAD_FUNNEL)

    for stmt in disable_tenant_rls_sql(_LISTING_STATS):
        op.execute(stmt)
    op.drop_index("ix_listing_stats_daily_tenant_day", table_name=_LISTING_STATS)
    op.drop_index(op.f("ix_listing_stats_daily_tenant_id"), table_name=_LISTING_STATS)
    op.drop_table(_LISTING_STATS)

    # Dropping the partitioned parent drops all its partitions with it.
    for stmt in disable_tenant_rls_sql(_EVENTS):
        op.execute(stmt)
    op.execute(f"DROP TABLE {_EVENTS}")
