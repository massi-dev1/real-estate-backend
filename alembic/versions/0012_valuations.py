"""valuation requests (§8.8, tenant RLS)

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-18

One tenant-owned table with the fail-closed tenant RLS policy:
``valuation_requests`` — the public multi-step seller-valuation form, filled
progressively (address → property details → contact + computed estimate band).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROPERTY_TYPE = sa.Enum(
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
)


def upgrade() -> None:
    op.create_table(
        "valuation_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "address", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "location",
            Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=True,
        ),
        sa.Column("property_type", PROPERTY_TYPE, nullable=True),
        sa.Column("area_built", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("beds", sa.SmallInteger(), nullable=True),
        sa.Column("baths", sa.SmallInteger(), nullable=True),
        sa.Column("floor", sa.SmallInteger(), nullable=True),
        sa.Column("year_built", sa.SmallInteger(), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        sa.Column("lead_id", sa.Uuid(), nullable=True),
        sa.Column("estimate_low", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("estimate_high", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="DZD", nullable=False),
        sa.Column("comps_count", sa.SmallInteger(), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            name=op.f("fk_valuation_requests_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name=op.f("fk_valuation_requests_contact_id_contacts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"],
            ["leads.id"],
            name=op.f("fk_valuation_requests_lead_id_leads"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_valuation_requests")),
    )
    op.create_index(
        op.f("ix_valuation_requests_tenant_id"), "valuation_requests", ["tenant_id"], unique=False
    )

    for stmt in enable_tenant_rls_sql("valuation_requests"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in disable_tenant_rls_sql("valuation_requests"):
        op.execute(stmt)

    op.drop_index(op.f("ix_valuation_requests_tenant_id"), table_name="valuation_requests")
    op.drop_table("valuation_requests")
