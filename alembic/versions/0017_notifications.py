"""notifications: in-app + multi-channel fan-out (§8.12, tenant RLS)

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-21

Four tenant-owned, RLS-protected tables (§8.12):
- ``notifications`` — the durable in-app row (``GET /me/notifications``).
- ``notification_preferences`` — one ``enabled`` boolean per (user, type,
  channel); a missing row means "use the type default".
- ``notification_sends`` — append-only per-channel delivery log (deliverability
  is debuggable). ``notification_id`` is nullable so a digest email (one send
  batching many notifications) is still loggable.
- ``notification_digest_items`` — the pending-digest queue drained by the
  batching Beat sweep (quiet-hours anti-spam).

Channels/types/statuses are non-native check-constrained varchars (same stance
as every other status enum here — no Postgres enum type to migrate as the set
grows).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOTIFICATION_TYPE = sa.Enum(
    "lead_assigned",
    "lead_escalated",
    "appointment_reminder",
    "appointment_confirmed",
    "appointment_cancelled",
    name="notification_type",
    native_enum=False,
    length=40,
)
NOTIFICATION_CHANNEL = sa.Enum(
    "in_app",
    "email",
    "sms",
    "whatsapp",
    name="notification_channel",
    native_enum=False,
    length=40,
)
SEND_STATUS = sa.Enum(
    "sent",
    "failed",
    "skipped",
    name="notification_send_status",
    native_enum=False,
    length=40,
)

_TENANT_TABLES = (
    "notifications",
    "notification_preferences",
    "notification_sends",
    "notification_digest_items",
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
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", NOTIFICATION_TYPE, nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_notifications_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
    )
    op.create_index(
        op.f("ix_notifications_tenant_id"), "notifications", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_notifications_user_id"), "notifications", ["user_id"], unique=False
    )
    op.create_index(
        "ix_notifications_tenant_user_created",
        "notifications",
        ["tenant_id", "user_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("type", NOTIFICATION_TYPE, nullable=False),
        sa.Column("channel", NOTIFICATION_CHANNEL, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_notification_preferences_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_preferences_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_preferences")),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "type",
            "channel",
            name="uq_notification_preferences_user_type_channel",
        ),
    )
    op.create_index(
        op.f("ix_notification_preferences_tenant_id"),
        "notification_preferences",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_preferences_user_id"),
        "notification_preferences",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_preferences_tenant_user",
        "notification_preferences",
        ["tenant_id", "user_id"],
        unique=False,
    )

    op.create_table(
        "notification_sends",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("notification_id", sa.Uuid(), nullable=True),
        sa.Column("channel", NOTIFICATION_CHANNEL, nullable=False),
        sa.Column("status", SEND_STATUS, nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_notification_sends_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name=op.f("fk_notification_sends_notification_id_notifications"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_sends")),
    )
    op.create_index(
        op.f("ix_notification_sends_tenant_id"),
        "notification_sends",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_sends_tenant_notification",
        "notification_sends",
        ["tenant_id", "notification_id"],
        unique=False,
    )

    op.create_table(
        "notification_digest_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("notification_id", sa.Uuid(), nullable=False),
        sa.Column("channel", NOTIFICATION_CHANNEL, nullable=False),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_notification_digest_items_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_digest_items_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name=op.f("fk_notification_digest_items_notification_id_notifications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_digest_items")),
    )
    op.create_index(
        op.f("ix_notification_digest_items_tenant_id"),
        "notification_digest_items",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_digest_items_user_id"),
        "notification_digest_items",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_digest_items_tenant_user_pending",
        "notification_digest_items",
        ["tenant_id", "user_id", "sent_at"],
        unique=False,
    )

    for table in _TENANT_TABLES:
        for stmt in enable_tenant_rls_sql(table):
            op.execute(stmt)


def downgrade() -> None:
    for table in _TENANT_TABLES:
        for stmt in disable_tenant_rls_sql(table):
            op.execute(stmt)

    op.drop_index(
        "ix_notification_digest_items_tenant_user_pending",
        table_name="notification_digest_items",
    )
    op.drop_index(
        op.f("ix_notification_digest_items_user_id"),
        table_name="notification_digest_items",
    )
    op.drop_index(
        op.f("ix_notification_digest_items_tenant_id"),
        table_name="notification_digest_items",
    )
    op.drop_table("notification_digest_items")

    op.drop_index(
        "ix_notification_sends_tenant_notification", table_name="notification_sends"
    )
    op.drop_index(op.f("ix_notification_sends_tenant_id"), table_name="notification_sends")
    op.drop_table("notification_sends")

    op.drop_index(
        "ix_notification_preferences_tenant_user",
        table_name="notification_preferences",
    )
    op.drop_index(
        op.f("ix_notification_preferences_user_id"),
        table_name="notification_preferences",
    )
    op.drop_index(
        op.f("ix_notification_preferences_tenant_id"),
        table_name="notification_preferences",
    )
    op.drop_table("notification_preferences")

    op.drop_index(
        "ix_notifications_tenant_user_created", table_name="notifications"
    )
    op.drop_index(op.f("ix_notifications_user_id"), table_name="notifications")
    op.drop_index(op.f("ix_notifications_tenant_id"), table_name="notifications")
    op.drop_table("notifications")
