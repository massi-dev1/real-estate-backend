"""listings, status history, reference counters (tenant RLS + PostGIS)

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-16

All three tables are strictly tenant-owned (NOT NULL ``tenant_id``) and get
the fail-closed tenant RLS policy. PostGIS is created here — the first
geometry column in the schema. Deferred to their own parts: ``search_vector``
+ FTS (search), ``neighborhood_id`` (content), media tables (media).
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LISTING_STATUS = sa.Enum(
    "draft",
    "review",
    "published",
    "reserved",
    "sold",
    "rented",
    "archived",
    name="listing_status",
    native_enum=False,
    length=20,
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "listings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("reference_code", sa.String(length=24), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("status", LISTING_STATUS, server_default="draft", nullable=False),
        sa.Column(
            "purpose",
            sa.Enum(
                "sale", "rent", "rent_daily", name="listing_purpose", native_enum=False, length=20
            ),
            nullable=False,
        ),
        sa.Column(
            "property_type",
            sa.Enum(
                "apartment",
                "house",
                "villa",
                "studio",
                "duplex",
                "land",
                "office",
                "retail",
                "warehouse",
                "garage",
                "farm",
                "building",
                "other",
                name="property_type",
                native_enum=False,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column("title", postgresql.JSONB(), nullable=False),
        sa.Column(
            "description",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("price", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="DZD", nullable=False),
        sa.Column(
            "price_period",
            sa.Enum("month", "day", name="price_period", native_enum=False, length=20),
            nullable=True,
        ),
        sa.Column("negotiable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("beds", sa.SmallInteger(), nullable=True),
        sa.Column("baths", sa.SmallInteger(), nullable=True),
        sa.Column("area_built", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("area_land", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("floor", sa.SmallInteger(), nullable=True),
        sa.Column("floors_total", sa.SmallInteger(), nullable=True),
        sa.Column("year_built", sa.SmallInteger(), nullable=True),
        sa.Column(
            "features",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "address",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "location",
            geoalchemy2.types.Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=True,
        ),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("view_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            name=op.f("fk_listings_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["users.id"],
            name=op.f("fk_listings_agent_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_listings_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_listings")),
        sa.UniqueConstraint("tenant_id", "reference_code", name=op.f("uq_listings_tenant_id")),
    )
    op.create_index(op.f("ix_listings_tenant_id"), "listings", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_listings_agent_id"), "listings", ["agent_id"], unique=False)
    # The two hot public-query paths (§6.3).
    op.create_index(
        "ix_listings_tenant_status_published",
        "listings",
        ["tenant_id", "status", sa.text("published_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_listings_tenant_purpose_type_price",
        "listings",
        ["tenant_id", "purpose", "property_type", "price"],
        unique=False,
    )
    op.create_index(
        "ix_listings_features", "listings", ["features"], unique=False, postgresql_using="gin"
    )
    op.create_index(
        "ix_listings_location", "listings", ["location"], unique=False, postgresql_using="gist"
    )

    op.create_table(
        "listing_status_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=False),
        sa.Column("from_status", LISTING_STATUS, nullable=False),
        sa.Column("to_status", LISTING_STATUS, nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
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
            name=op.f("fk_listing_status_history_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_listing_status_history_listing_id_listings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by"],
            ["users.id"],
            name=op.f("fk_listing_status_history_changed_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_listing_status_history")),
    )
    op.create_index(
        op.f("ix_listing_status_history_tenant_id"),
        "listing_status_history",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_listing_status_history_listing_id"),
        "listing_status_history",
        ["listing_id"],
        unique=False,
    )

    op.create_table(
        "listing_reference_counters",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("last_value", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_listing_reference_counters_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "year", name=op.f("pk_listing_reference_counters")),
    )

    for table in ("listings", "listing_status_history", "listing_reference_counters"):
        for stmt in enable_tenant_rls_sql(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in ("listing_reference_counters", "listing_status_history", "listings"):
        for stmt in disable_tenant_rls_sql(table):
            op.execute(stmt)
    op.drop_table("listing_reference_counters")
    op.drop_index(op.f("ix_listing_status_history_listing_id"), table_name="listing_status_history")
    op.drop_index(op.f("ix_listing_status_history_tenant_id"), table_name="listing_status_history")
    op.drop_table("listing_status_history")
    op.drop_index("ix_listings_location", table_name="listings")
    op.drop_index("ix_listings_features", table_name="listings")
    op.drop_index("ix_listings_tenant_purpose_type_price", table_name="listings")
    op.drop_index("ix_listings_tenant_status_published", table_name="listings")
    op.drop_index(op.f("ix_listings_agent_id"), table_name="listings")
    op.drop_index(op.f("ix_listings_tenant_id"), table_name="listings")
    op.drop_table("listings")
    # PostGIS stays installed: extensions are shared, other revisions may rely
    # on it, and DROP EXTENSION would fail if anything else uses geometry.
