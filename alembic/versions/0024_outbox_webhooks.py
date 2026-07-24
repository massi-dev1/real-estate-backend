"""outbox + outbound webhooks (tenant RLS)

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-24

Part 31 (§12, §8.14, §10.9). Three tenant-owned tables, all fail-closed RLS:

- ``outbox`` — the transactional-outbox event log (§12): a domain event written
  in the same transaction as the change that produced it, drained by a Beat
  relay. Status/attempts/next_attempt_at drive at-least-once delivery with
  exponential backoff; a poison row lands ``failed`` after N attempts.
- ``webhook_endpoints`` — tenant-registered outbound webhook targets (URL + HMAC
  secret + subscribed events), with the same circuit-breaker columns
  ``portal_sync_state`` uses (§8.14).
- ``webhook_deliveries`` — the append-only per-attempt delivery log (§10.9).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OUTBOX_STATUS = sa.Enum(
    "pending", "delivered", "failed", name="outbox_status", native_enum=False, length=20
)
DELIVERY_STATUS = sa.Enum(
    "pending",
    "delivered",
    "failed",
    name="webhook_delivery_status",
    native_enum=False,
    length=20,
)


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    # ---- outbox ----
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", OUTBOX_STATUS, server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("dispatched_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_outbox_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
    )
    op.create_index(op.f("ix_outbox_tenant_id"), "outbox", ["tenant_id"], unique=False)
    # The relay's hot query: due pending rows for a tenant, oldest first.
    op.create_index(
        "ix_outbox_due", "outbox", ["tenant_id", "status", "next_attempt_at"], unique=False
    )
    for stmt in enable_tenant_rls_sql("outbox"):
        op.execute(stmt)

    # ---- webhook_endpoints ----
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2000), nullable=False),
        sa.Column("secret", sa.String(length=255), nullable=False),
        sa.Column("events", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("circuit_open", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_webhook_endpoints_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_endpoints")),
    )
    op.create_index(
        op.f("ix_webhook_endpoints_tenant_id"), "webhook_endpoints", ["tenant_id"], unique=False
    )
    for stmt in enable_tenant_rls_sql("webhook_endpoints"):
        op.execute(stmt)

    # ---- webhook_deliveries ----
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", DELIVERY_STATUS, server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_webhook_deliveries_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["webhook_endpoints.id"],
            name=op.f("fk_webhook_deliveries_endpoint_id_webhook_endpoints"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
    )
    op.create_index(
        op.f("ix_webhook_deliveries_tenant_id"), "webhook_deliveries", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_webhook_deliveries_endpoint_id"),
        "webhook_deliveries",
        ["endpoint_id"],
        unique=False,
    )
    for stmt in enable_tenant_rls_sql("webhook_deliveries"):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in disable_tenant_rls_sql("webhook_deliveries"):
        op.execute(stmt)
    op.drop_index(op.f("ix_webhook_deliveries_endpoint_id"), table_name="webhook_deliveries")
    op.drop_index(op.f("ix_webhook_deliveries_tenant_id"), table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")

    for stmt in disable_tenant_rls_sql("webhook_endpoints"):
        op.execute(stmt)
    op.drop_index(op.f("ix_webhook_endpoints_tenant_id"), table_name="webhook_endpoints")
    op.drop_table("webhook_endpoints")

    for stmt in disable_tenant_rls_sql("outbox"):
        op.execute(stmt)
    op.drop_index("ix_outbox_due", table_name="outbox")
    op.drop_index(op.f("ix_outbox_tenant_id"), table_name="outbox")
    op.drop_table("outbox")
