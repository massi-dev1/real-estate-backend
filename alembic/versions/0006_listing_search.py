"""listing search: featured flag, generated search_vector + FTS/sort indexes

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-16

§8.3: keyword search runs on a STORED generated tsvector combining every
locale with its own text-search config (french/english/arabic ship with
PG 12+), weighted title > description > city. Generated columns only allow
immutable expressions — ``to_tsvector(regconfig, text)`` and ``jsonb ->> text``
both are, so the vector maintains itself on every write with no trigger.

``featured`` backs the paid-placement sort boost; it leads every public sort,
so the hot public index gains it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('french', coalesce(title ->> 'fr', '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(title ->> 'en', '')), 'A') || "
    "setweight(to_tsvector('arabic', coalesce(title ->> 'ar', '')), 'A') || "
    "setweight(to_tsvector('french', coalesce(description ->> 'fr', '')), 'B') || "
    "setweight(to_tsvector('english', coalesce(description ->> 'en', '')), 'B') || "
    "setweight(to_tsvector('arabic', coalesce(description ->> 'ar', '')), 'B') || "
    "setweight(to_tsvector('simple', coalesce(address ->> 'city', '')), 'C')"
)


def upgrade() -> None:
    op.add_column(
        "listings",
        sa.Column("featured", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute(
        "ALTER TABLE listings ADD COLUMN search_vector tsvector "
        f"GENERATED ALWAYS AS ({SEARCH_VECTOR_SQL}) STORED"
    )
    op.create_index(
        "ix_listings_search_vector",
        "listings",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    # Every public sort leads (featured DESC, <key>): give the default
    # newest-first path a covering keyset index.
    op.create_index(
        "ix_listings_tenant_status_featured_published",
        "listings",
        ["tenant_id", "status", sa.text("featured DESC"), sa.text("published_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_listings_tenant_status_featured_published", table_name="listings")
    op.drop_index("ix_listings_search_vector", table_name="listings")
    op.drop_column("listings", "search_vector")
    op.drop_column("listings", "featured")
