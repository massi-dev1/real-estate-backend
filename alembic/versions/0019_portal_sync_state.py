"""portal syndication: per-listing per-portal sync state (§8.14, tenant RLS)

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-21

One tenant-owned, RLS-protected table ``portal_sync_state`` recording, for each
(listing, portal) pair, what we last pushed and how it went — the source of
truth for the portal admin's sync view and the circuit breaker.

- ``listing_id`` links into listings **by column only** (FK for integrity +
  ``ON DELETE CASCADE`` so a purged listing's sync rows go with it) — the
  syndication service reaches listings through their service boundary, never
  their table.
- ``portal_key`` is a code-owned adapter key (``KNOWN_PORTALS``); the natural
  key is the ``(tenant_id, listing_id, portal_key)`` unique constraint.
- Circuit breaker (§8.14): ``consecutive_failures`` + ``circuit_open`` pause a
  broken portal-tenant pair after N failures instead of retry-storming.

``last_status`` is a non-native check-constrained varchar (codebase standard —
no Postgres enum type to migrate).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYNC_STATUS = sa.Enum(
    "pending",
    "synced",
    "removed",
    "failed",
    "paused",
    name="portal_sync_status",
    native_enum=False,
    length=20,
)

_TABLE = "portal_sync_state"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("listing_id", sa.Uuid(), nullable=False),
        sa.Column("portal_key", sa.String(length=40), nullable=False),
        sa.Column("remote_id", sa.String(length=255), nullable=True),
        sa.Column("last_status", SYNC_STATUS, nullable=False),
        sa.Column("last_pushed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "circuit_open", sa.Boolean(), nullable=False, server_default=sa.text("false")
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_portal_sync_state_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["listings.id"],
            name=op.f("fk_portal_sync_state_listing_id_listings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portal_sync_state")),
        sa.UniqueConstraint(
            "tenant_id", "listing_id", "portal_key", name="uq_portal_sync_listing_portal"
        ),
    )
    op.create_index(
        op.f("ix_portal_sync_state_tenant_id"), _TABLE, ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_portal_sync_state_listing_id"), _TABLE, ["listing_id"], unique=False
    )
    # The admin list pages by (updated_at DESC, id DESC), optionally per portal.
    op.create_index(
        "ix_portal_sync_state_tenant_updated",
        _TABLE,
        ["tenant_id", "updated_at"],
        unique=False,
    )

    for stmt in enable_tenant_rls_sql(_TABLE):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in disable_tenant_rls_sql(_TABLE):
        op.execute(stmt)

    op.drop_index("ix_portal_sync_state_tenant_updated", table_name=_TABLE)
    op.drop_index(op.f("ix_portal_sync_state_listing_id"), table_name=_TABLE)
    op.drop_index(op.f("ix_portal_sync_state_tenant_id"), table_name=_TABLE)
    op.drop_table(_TABLE)
