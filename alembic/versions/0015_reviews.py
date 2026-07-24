"""reviews: moderated agent/tenant testimonials (§8.11, tenant RLS)

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-21

One tenant-owned table with the fail-closed tenant RLS policy:
- ``reviews`` — a rating (1-5) + free-text body about an agent (nullable
  ``agent_user_id`` — NULL is a tenant-wide testimonial) with an optional
  ``listing_id`` for context. Public submissions land ``pending`` and move to
  ``approved``/``rejected`` through the moderation queue; only approved rows
  feed the public aggregates. ``listing_id`` FKs ``ON DELETE SET NULL`` so a
  deleted listing does not cascade-delete the review it was attached to.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVIEW_STATUS = sa.Enum(
    "pending",
    "approved",
    "rejected",
    name="review_status",
    native_enum=False,
    length=20,
)


def upgrade() -> None:
    op.create_table(
        "reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        # NULL agent = a tenant-wide testimonial (agency review, not an agent's).
        sa.Column("agent_user_id", sa.Uuid(), nullable=True),
        sa.Column("listing_id", sa.Uuid(), nullable=True),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("body", sa.String(length=4000), nullable=False),
        sa.Column("author_name", sa.String(length=120), nullable=False),
        sa.Column("author_email", sa.String(length=320), nullable=True),
        sa.Column("status", REVIEW_STATUS, server_default="pending", nullable=False),
        # Set by the moderator on approval (or a future verified-client path):
        # the review reflects a real, confirmed relationship.
        sa.Column("is_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("moderated_by", sa.Uuid(), nullable=True),
        sa.Column("moderated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("moderation_note", sa.String(length=500), nullable=True),
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
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_reviews_rating_range"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_reviews_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["agent_user_id"],
            ["users.id"],
            name=op.f("fk_reviews_agent_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_reviews_listing_id_listings"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["moderated_by"],
            ["users.id"],
            name=op.f("fk_reviews_moderated_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
    )
    op.create_index(op.f("ix_reviews_tenant_id"), "reviews", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_reviews_agent_user_id"), "reviews", ["agent_user_id"], unique=False)
    op.create_index(op.f("ix_reviews_listing_id"), "reviews", ["listing_id"], unique=False)
    # Covers the moderation queue (portal) and the public/aggregate reads:
    # filter by status, newest first, optionally per agent.
    op.create_index(
        "ix_reviews_tenant_status_agent_created",
        "reviews",
        ["tenant_id", "status", "agent_user_id", "created_at"],
        unique=False,
    )

    for stmt in enable_tenant_rls_sql("reviews"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in disable_tenant_rls_sql("reviews"):
        op.execute(stmt)

    op.drop_index("ix_reviews_tenant_status_agent_created", table_name="reviews")
    op.drop_index(op.f("ix_reviews_listing_id"), table_name="reviews")
    op.drop_index(op.f("ix_reviews_agent_user_id"), table_name="reviews")
    op.drop_index(op.f("ix_reviews_tenant_id"), table_name="reviews")
    op.drop_table("reviews")
