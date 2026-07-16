"""listing_media (§6.3, §8.2 media pipeline) — tenant RLS, one-cover index

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "listing_media",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "photo",
                "video",
                "tour_3d",
                "floorplan",
                "doc",
                name="media_kind",
                native_enum=False,
                length=20,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "processing",
                "ready",
                "failed",
                name="media_status",
                native_enum=False,
                length=20,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(length=300), nullable=True),
        sa.Column("embed_url", sa.String(length=500), nullable=True),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "variants",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("blurhash", sa.String(length=60), nullable=True),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "alt_text",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_cover", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("error", sa.String(length=200), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
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
            name=op.f("fk_listing_media_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_listing_media_listing_id_listings"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_listing_media_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_listing_media")),
    )
    op.create_index(
        "ix_listing_media_tenant_listing",
        "listing_media",
        ["tenant_id", "listing_id", "position"],
        unique=False,
    )
    # At most one cover per listing (partial unique).
    op.create_index(
        "uq_listing_media_cover",
        "listing_media",
        ["tenant_id", "listing_id"],
        unique=True,
        postgresql_where=sa.text("is_cover"),
    )

    for stmt in enable_tenant_rls_sql("listing_media"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in disable_tenant_rls_sql("listing_media"):
        op.execute(stmt)
    op.drop_index("uq_listing_media_cover", table_name="listing_media")
    op.drop_index("ix_listing_media_tenant_listing", table_name="listing_media")
    op.drop_table("listing_media")
