"""partial indexes for the platform-staff (tenant_id IS NULL) identity partition

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-31

Audit finding DB-01. The three identity tables (``users``, ``sessions``,
``oauth_identities``) carry a nullable ``tenant_id`` — NULL means platform staff
(§7.2) — and their RLS policy is ``tenant_id IS NOT DISTINCT FROM
NULLIF(current_setting('app.tenant_id', true), '')::uuid``, so an unscoped
platform request resolves to ``tenant_id IS NULL``.

The existing plain B-tree on ``tenant_id`` *does* index NULLs, so this is not a
correctness or a seq-scan fix. It is a size-and-selectivity one: platform rows
are a tiny fraction of these tables (a handful of staff against every tenant's
users and every live session), and as the tables grow into millions of rows the
planner is reading NULL entries out of an index dominated by tenant UUIDs. A
partial index contains *only* the platform partition, so it stays a few pages
regardless of how many tenants sign up, and the ``WHERE tenant_id IS NULL``
predicate matches the RLS-rewritten query exactly.

Built ``CONCURRENTLY``: these tables are hot (every authenticated request
touches ``users``), and a plain CREATE INDEX takes an ACCESS EXCLUSIVE lock that
would stall auth for the duration on a large table. That requires running
outside a transaction — hence the ``autocommit_block``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (index name, table). One per identity table using the nullable-tenant RLS
# policy — keep in sync with the tables passed to ``enable_identity_rls_sql``.
_PLATFORM_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_users_platform_partition", "users"),
    ("ix_sessions_platform_partition", "sessions"),
    ("ix_oauth_identities_platform_partition", "oauth_identities"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table in _PLATFORM_INDEXES:
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                f"ON {table} (tenant_id) WHERE tenant_id IS NULL"
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _table in _PLATFORM_INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
