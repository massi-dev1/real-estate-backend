"""blog: categories + posts (§8.10 slice 2, tenant RLS)

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-20

Two tenant-owned tables with the fail-closed tenant RLS policy:
- ``blog_categories`` — small curated taxonomy (i18n name, unique slug per tenant).
- ``blog_posts`` — i18n title/excerpt/body (sanitized HTML), GIN-indexed
  ``tags``, and a draft/scheduled/published lifecycle. ``category_id`` FKs to
  ``blog_categories`` with ``ON DELETE SET NULL`` so deleting a category does
  not cascade-delete its posts.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BLOG_POST_STATUS = sa.Enum(
    "draft",
    "scheduled",
    "published",
    name="blog_post_status",
    native_enum=False,
    length=30,
)


def upgrade() -> None:
    op.create_table(
        "blog_categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("name", postgresql.JSONB(), nullable=False),
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
            name=op.f("fk_blog_categories_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blog_categories")),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_blog_categories_tenant_slug"),
    )
    op.create_index(
        op.f("ix_blog_categories_tenant_id"), "blog_categories", ["tenant_id"], unique=False
    )

    op.create_table(
        "blog_posts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title", postgresql.JSONB(), nullable=False),
        sa.Column("excerpt", postgresql.JSONB(), nullable=True),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column(
            "tags", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("cover_image", sa.String(length=500), nullable=True),
        sa.Column(
            "seo_meta", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("status", BLOG_POST_STATUS, server_default="draft", nullable=False),
        sa.Column("scheduled_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
            name=op.f("fk_blog_posts_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["blog_categories.id"],
            name=op.f("fk_blog_posts_category_id_blog_categories"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blog_posts")),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_blog_posts_tenant_slug"),
    )
    op.create_index(op.f("ix_blog_posts_tenant_id"), "blog_posts", ["tenant_id"], unique=False)
    op.create_index(
        op.f("ix_blog_posts_category_id"), "blog_posts", ["category_id"], unique=False
    )
    op.create_index(
        "ix_blog_posts_tenant_status_published",
        "blog_posts",
        ["tenant_id", "status", "published_at"],
        unique=False,
    )
    op.create_index(
        "ix_blog_posts_tags", "blog_posts", ["tags"], unique=False, postgresql_using="gin"
    )

    for table in ("blog_categories", "blog_posts"):
        for stmt in enable_tenant_rls_sql(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in ("blog_posts", "blog_categories"):
        for stmt in disable_tenant_rls_sql(table):
            op.execute(stmt)

    op.drop_index("ix_blog_posts_tags", table_name="blog_posts")
    op.drop_index("ix_blog_posts_tenant_status_published", table_name="blog_posts")
    op.drop_index(op.f("ix_blog_posts_category_id"), table_name="blog_posts")
    op.drop_index(op.f("ix_blog_posts_tenant_id"), table_name="blog_posts")
    op.drop_table("blog_posts")

    op.drop_index(op.f("ix_blog_categories_tenant_id"), table_name="blog_categories")
    op.drop_table("blog_categories")
