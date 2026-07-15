"""tenants and tenant_domains (global platform tables)

Revision ID: 0001
Revises:
Create Date: 2026-07-15

These are platform-level tables (§4.3) — deliberately *not* under RLS, since
the tenant-resolution middleware queries them before any tenant context
exists. Tenant-owned tables added from Part 3 on must call
``app.core.rls.enable_tenant_rls_sql`` in their migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "trial",
                "active",
                "suspended",
                name="tenant_status",
                native_enum=False,
                length=20,
            ),
            server_default="trial",
            nullable=False,
        ),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
        sa.UniqueConstraint("slug", name=op.f("uq_tenants_slug")),
    )
    op.create_table(
        "tenant_domains",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            name=op.f("fk_tenant_domains_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_domains")),
        sa.UniqueConstraint("domain", name=op.f("uq_tenant_domains_domain")),
    )
    op.create_index(
        op.f("ix_tenant_domains_tenant_id"), "tenant_domains", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tenant_domains_tenant_id"), table_name="tenant_domains")
    op.drop_table("tenant_domains")
    op.drop_table("tenants")
