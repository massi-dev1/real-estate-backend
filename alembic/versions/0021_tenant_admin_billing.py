"""tenant administration & billing (§8.16)

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-23

Extends Part 2's platform tables and adds the billing/admin machinery. All of
these are **global** (platform-level, no RLS) — the same stance as ``tenants``
itself (§4.3): the tenant-resolution middleware and the platform back-office
query them without a tenant context, and billing/audit are platform concerns
even though they reference a ``tenant_id``.

- ``tenants`` gains lifecycle columns: ``plan`` (quota tier), ``trial_ends_at``
  (trial-expiry sweep), and offboard columns (``offboarding_at`` /
  ``deletion_scheduled_at`` / ``deleted_at``) driving export-then-delete.
- ``tenant_domains`` gains DNS-verification columns (``verification_token`` /
  ``verification_status``) — the *data model + API* for a TXT-record challenge;
  the actual on-demand-TLS wiring is ops-side and consumes ``verified_at``.
- ``tenant_usage`` — one row per tenant holding running quota counters
  (listings/agents/storage/monthly-emails) incremented at write-time, so a
  quota check is an O(1) read, never a recompute scan.
- ``tenant_subscriptions`` — the billing subscription mirror (provider ids,
  status, period end, dunning grace window).
- ``billing_events`` — append-only webhook idempotency log (unique per
  ``(provider, event_id)``) per the §10.9 webhook-hardening rules.
- ``audit_log`` — minimal append-only audit trail; this part needs it for
  impersonation (§10.11), Part 23/compliance broadens it later.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOMAIN_VERIFICATION_STATUS = sa.Enum(
    "pending",
    "verified",
    "failed",
    name="domain_verification_status",
    native_enum=False,
    length=20,
)
SUBSCRIPTION_STATUS = sa.Enum(
    "trialing",
    "active",
    "past_due",
    "canceled",
    "incomplete",
    name="subscription_status",
    native_enum=False,
    length=20,
)


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
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
    )


def upgrade() -> None:
    # ---- tenants: plan tier + trial + offboard lifecycle ----
    op.add_column(
        "tenants",
        sa.Column("plan", sa.String(length=40), server_default="trial", nullable=False),
    )
    op.add_column("tenants", sa.Column("trial_ends_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(
        "tenants", sa.Column("offboarding_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column(
        "tenants",
        sa.Column("deletion_scheduled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column("tenants", sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True))
    # The offboard export archive's private-bucket key (§8.16) — a presigned GET
    # off this lets the departing tenant download their data before the purge.
    op.add_column("tenants", sa.Column("export_object_key", sa.String(length=500), nullable=True))

    # ---- tenant_domains: DNS TXT-challenge verification ----
    op.add_column(
        "tenant_domains",
        sa.Column("verification_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "tenant_domains",
        sa.Column(
            "verification_status",
            DOMAIN_VERIFICATION_STATUS,
            server_default="pending",
            nullable=False,
        ),
    )

    # ---- tenant_usage: O(1) running quota counters (§8.16) ----
    op.create_table(
        "tenant_usage",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("listings_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("agents_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("storage_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        # Monthly email counter + the month it belongs to ("YYYY-MM"); a new
        # month resets the counter on the next increment (checked in-service).
        sa.Column("emails_sent", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("emails_period", sa.String(length=7), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("listings_count >= 0", name="ck_tenant_usage_listings_nonneg"),
        sa.CheckConstraint("agents_count >= 0", name="ck_tenant_usage_agents_nonneg"),
        sa.CheckConstraint("storage_bytes >= 0", name="ck_tenant_usage_storage_nonneg"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_usage_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_tenant_usage")),
    )

    # ---- tenant_subscriptions: billing mirror ----
    op.create_table(
        "tenant_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("plan", sa.String(length=40), nullable=False),
        sa.Column("status", SUBSCRIPTION_STATUS, nullable=False),
        sa.Column("provider_customer_id", sa.String(length=255), nullable=True),
        sa.Column("provider_subscription_id", sa.String(length=255), nullable=True),
        sa.Column("current_period_end", sa.TIMESTAMP(timezone=True), nullable=True),
        # Dunning: when a payment fails the subscription enters past_due and
        # stays reachable until grace_until; the sweep suspends past it.
        sa.Column("grace_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "cancel_at_period_end",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_subscriptions_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_subscriptions")),
        # One live subscription record per tenant (v1 — single plan per agency).
        sa.UniqueConstraint("tenant_id", name=op.f("uq_tenant_subscriptions_tenant_id")),
    )
    op.create_index(
        op.f("ix_tenant_subscriptions_provider_subscription_id"),
        "tenant_subscriptions",
        ["provider_subscription_id"],
        unique=False,
    )

    # ---- billing_events: webhook idempotency log (§10.9) ----
    op.create_table(
        "billing_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_events")),
        # Idempotency by (provider, event_id): a replayed webhook is a no-op.
        sa.UniqueConstraint(
            "provider", "event_id", name=op.f("uq_billing_events_provider_event_id")
        ),
    )

    # ---- audit_log: minimal append-only trail (§10.11) ----
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        # Nullable tenant_id: platform-level actions (impersonation start,
        # tenant suspend) reference a target tenant, but the row is global.
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_role", sa.String(length=40), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_audit_log_tenant_id_tenants"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(
        "ix_audit_log_tenant_created",
        "audit_log",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_log_action_created", "audit_log", ["action", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_action_created", table_name="audit_log")
    op.drop_index("ix_audit_log_tenant_created", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_table("billing_events")

    op.drop_index(
        op.f("ix_tenant_subscriptions_provider_subscription_id"),
        table_name="tenant_subscriptions",
    )
    op.drop_table("tenant_subscriptions")

    op.drop_table("tenant_usage")

    op.drop_column("tenant_domains", "verification_status")
    op.drop_column("tenant_domains", "verification_token")

    op.drop_column("tenants", "export_object_key")
    op.drop_column("tenants", "deleted_at")
    op.drop_column("tenants", "deletion_scheduled_at")
    op.drop_column("tenants", "offboarding_at")
    op.drop_column("tenants", "trial_ends_at")
    op.drop_column("tenants", "plan")
