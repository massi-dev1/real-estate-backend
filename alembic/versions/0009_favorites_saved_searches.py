"""favorites and saved searches (tenant RLS)

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-17

Both tables are strictly tenant-owned and get the fail-closed tenant RLS
policy. ``saved_searches`` is owned by exactly one of ``user_id`` (account)
or ``email`` (anonymous double-opt-in signup, §8.9) — CHECK enforced.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ALERT_FREQUENCY = sa.Enum(
    "instant",
    "daily",
    "weekly",
    name="alert_frequency",
    native_enum=False,
    length=20,
)


def upgrade() -> None:
    op.create_table(
        "favorites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=False),
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
            name=op.f("fk_favorites_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_favorites_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_favorites_listing_id_listings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_favorites")),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "listing_id", name=op.f("uq_favorites_tenant_id")
        ),
    )
    op.create_index(op.f("ix_favorites_tenant_id"), "favorites", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_favorites_listing_id"), "favorites", ["listing_id"], unique=False)
    op.create_index(
        "ix_favorites_tenant_user_created",
        "favorites",
        ["tenant_id", "user_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "saved_searches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "filters", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("locale", sa.String(length=10), server_default="fr", nullable=False),
        sa.Column("frequency", ALERT_FREQUENCY, server_default="instant", nullable=False),
        sa.Column("last_run_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.CheckConstraint(
            "(user_id IS NULL) <> (email IS NULL)",
            name=op.f("ck_saved_searches_owner_xor_email"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_saved_searches_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_saved_searches_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_searches")),
    )
    op.create_index(
        op.f("ix_saved_searches_tenant_id"), "saved_searches", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_saved_searches_tenant_active_freq",
        "saved_searches",
        ["tenant_id", "is_active", "frequency"],
        unique=False,
    )

    for table in ("favorites", "saved_searches"):
        for stmt in enable_tenant_rls_sql(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in ("saved_searches", "favorites"):
        for stmt in disable_tenant_rls_sql(table):
            op.execute(stmt)

    op.drop_index("ix_saved_searches_tenant_active_freq", table_name="saved_searches")
    op.drop_index(op.f("ix_saved_searches_tenant_id"), table_name="saved_searches")
    op.drop_table("saved_searches")

    op.drop_index("ix_favorites_tenant_user_created", table_name="favorites")
    op.drop_index(op.f("ix_favorites_listing_id"), table_name="favorites")
    op.drop_index(op.f("ix_favorites_tenant_id"), table_name="favorites")
    op.drop_table("favorites")
