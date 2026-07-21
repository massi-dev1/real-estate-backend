"""content CMS: pages + versioned legal pages (§8.10, tenant RLS)

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-20

Two tenant-owned tables with the fail-closed tenant RLS policy:
- ``content_pages`` — structured agency-site pages (i18n title, validated
  block JSON, SEO meta, draft/published), unique slug per tenant.
- ``legal_pages`` — append-only versioned legal documents; a partial-unique
  index keeps exactly one ``is_current`` row per (tenant, kind).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAGE_STATUS = sa.Enum("draft", "published", name="page_status", native_enum=False, length=30)
LEGAL_KIND = sa.Enum(
    "privacy",
    "terms",
    "fair_treatment",
    "license_disclosure",
    name="legal_kind",
    native_enum=False,
    length=30,
)


def upgrade() -> None:
    op.create_table(
        "content_pages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title", postgresql.JSONB(), nullable=False),
        sa.Column(
            "blocks", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column(
            "seo_meta", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("status", PAGE_STATUS, server_default="draft", nullable=False),
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
            name=op.f("fk_content_pages_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_pages")),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_content_pages_tenant_slug"),
    )
    op.create_index(
        op.f("ix_content_pages_tenant_id"), "content_pages", ["tenant_id"], unique=False
    )

    op.create_table(
        "legal_pages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", LEGAL_KIND, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column("effective_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
            name=op.f("fk_legal_pages_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_legal_pages")),
        sa.UniqueConstraint(
            "tenant_id", "kind", "version", name="uq_legal_pages_tenant_kind_version"
        ),
    )
    op.create_index(op.f("ix_legal_pages_tenant_id"), "legal_pages", ["tenant_id"], unique=False)
    # At most one current version per kind.
    op.create_index(
        "uq_legal_pages_current",
        "legal_pages",
        ["tenant_id", "kind"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    for table in ("content_pages", "legal_pages"):
        for stmt in enable_tenant_rls_sql(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in ("legal_pages", "content_pages"):
        for stmt in disable_tenant_rls_sql(table):
            op.execute(stmt)

    op.drop_index("uq_legal_pages_current", table_name="legal_pages")
    op.drop_index(op.f("ix_legal_pages_tenant_id"), table_name="legal_pages")
    op.drop_table("legal_pages")

    op.drop_index(op.f("ix_content_pages_tenant_id"), table_name="content_pages")
    op.drop_table("content_pages")
