"""content slice 3: neighborhood guides + market reports (§8.10 tail, tenant RLS)

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-21

Two tenant-owned tables with the fail-closed tenant RLS policy:
- ``neighborhood_guides`` — i18n name/body, per-tenant-unique slug, an optional
  PostGIS ``boundary`` MultiPolygon (GiST-indexed) whose contained published
  listings are auto-linked live via ``ST_Contains`` (never a stored FK), and
  worker-computed ``stats`` (listing count + median price).
- ``market_reports`` — i18n title, per-tenant-unique slug, author-supplied
  ``stats``, and a private-bucket ``pdf_object_key`` rendered off-thread. The
  PDF is gated: a public download endpoint takes an email → mints a lead →
  returns a short-lived presigned GET.
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAGE_STATUS = sa.Enum(
    "draft",
    "published",
    name="page_status",
    native_enum=False,
    length=30,
)
REPORT_STATUS = sa.Enum(
    "draft",
    "published",
    "ready",
    name="report_status",
    native_enum=False,
    length=30,
)


def upgrade() -> None:
    op.create_table(
        "neighborhood_guides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("name", postgresql.JSONB(), nullable=False),
        sa.Column(
            "body", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "boundary",
            geoalchemy2.types.Geometry(
                geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False
            ),
            nullable=True,
        ),
        sa.Column(
            "seo_meta", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        # page_status already exists from migration 0013 (content_pages); reuse
        # it rather than minting a second identical check constraint enum.
        sa.Column(
            "status",
            PAGE_STATUS,
            server_default="draft",
            nullable=False,
        ),
        sa.Column(
            "stats", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("stats_computed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            name=op.f("fk_neighborhood_guides_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_neighborhood_guides")),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_neighborhood_guides_tenant_slug"),
    )
    op.create_index(
        op.f("ix_neighborhood_guides_tenant_id"),
        "neighborhood_guides",
        ["tenant_id"],
        unique=False,
    )
    # ST_Contains(boundary, listing.location) over published guides.
    op.create_index(
        "ix_neighborhood_guides_boundary",
        "neighborhood_guides",
        ["boundary"],
        unique=False,
        postgresql_using="gist",
    )

    op.create_table(
        "market_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title", postgresql.JSONB(), nullable=False),
        sa.Column(
            "stats", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("pdf_object_key", sa.String(length=300), nullable=True),
        sa.Column("status", REPORT_STATUS, server_default="draft", nullable=False),
        sa.Column("generated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            name=op.f("fk_market_reports_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_reports")),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_market_reports_tenant_slug"),
    )
    op.create_index(
        op.f("ix_market_reports_tenant_id"), "market_reports", ["tenant_id"], unique=False
    )

    for table in ("neighborhood_guides", "market_reports"):
        for stmt in enable_tenant_rls_sql(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in ("market_reports", "neighborhood_guides"):
        for stmt in disable_tenant_rls_sql(table):
            op.execute(stmt)

    op.drop_index(op.f("ix_market_reports_tenant_id"), table_name="market_reports")
    op.drop_table("market_reports")

    op.drop_index("ix_neighborhood_guides_boundary", table_name="neighborhood_guides")
    op.drop_index(op.f("ix_neighborhood_guides_tenant_id"), table_name="neighborhood_guides")
    op.drop_table("neighborhood_guides")
