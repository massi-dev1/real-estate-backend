"""Notifications (§8.12) — the unified in-app + multi-channel fan-out, tenant-
owned and RLS-protected (migration 0017).

Four tables:

- ``notifications`` — the in-app row (what ``GET /me/notifications`` lists):
  ``user_id``, a ``type`` from a code-owned allowlist, an opaque ``payload``
  JSONB (per-type template variables), and a nullable ``read_at`` watermark.
- ``notification_preferences`` — one row per (user, type, channel) with an
  ``enabled`` boolean. A *missing* row means "use the type's default channel
  set" (see ``notifications.types``), so an untouched user still gets sensible
  delivery without a backfill.
- ``notification_sends`` — append-only per-channel delivery log (the §8.12
  point-4 "deliverability is debuggable" requirement): which channel, the
  provider message id, status, error, when. ``notification_id`` is nullable so
  a digest email (which batches many notifications into one send) can still be
  logged.
- ``notification_digest_items`` — the pending-digest queue: a digest-eligible
  notification, instead of sending its email immediately, drops a row here for
  the Beat sweep to batch into one email per user. Cleared (``sent_at`` stamped)
  once batched.

Channels and types are ``StrEnum``s stored as non-native check-constrained
varchars (same stance as every other status enum in the codebase — no Postgres
type to migrate when the set grows).
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class NotificationChannel(enum.StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"


# The external (non in-app) channels a send task can be enqueued for.
EXTERNAL_CHANNELS: tuple[NotificationChannel, ...] = (
    NotificationChannel.EMAIL,
    NotificationChannel.SMS,
    NotificationChannel.WHATSAPP,
)


class NotificationType(enum.StrEnum):
    """The allowlist every ``notify()`` call must name — a template + default
    channel set is registered per type in ``notifications.types``. Adding a
    type is a code change (auditable in git), never free-form user input."""

    LEAD_ASSIGNED = "lead_assigned"  # speed-to-lead (§8.4) → the assigned agent
    LEAD_ESCALATED = "lead_escalated"  # unassigned too long (§8.4) → admins
    APPOINTMENT_REMINDER = "appointment_reminder"  # tour reminder (§8.7) → contact
    APPOINTMENT_CONFIRMED = "appointment_confirmed"  # (§8.7) → contact
    APPOINTMENT_CANCELLED = "appointment_cancelled"  # (§8.7) → contact


class SendStatus(enum.StrEnum):
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"  # no adapter configured (sms/whatsapp) — logged, not sent


def _str_enum(enum_cls: type[enum.StrEnum], name: str, length: int = 40) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        # The `/me` list: a user's notifications, newest first, unread-first
        # counting is a WHERE read_at IS NULL over the same index.
        Index("ix_notifications_tenant_user_created", "tenant_id", "user_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[NotificationType] = mapped_column(
        _str_enum(NotificationType, "notification_type")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    read_at: Mapped[datetime | None]


class NotificationPreference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notification_preferences"
    __table_args__ = (
        # One decision per (user, type, channel); a missing row = the type default.
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "type",
            "channel",
            name="uq_notification_preferences_user_type_channel",
        ),
        Index("ix_notification_preferences_tenant_user", "tenant_id", "user_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[NotificationType] = mapped_column(
        _str_enum(NotificationType, "notification_type")
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        _str_enum(NotificationChannel, "notification_channel")
    )
    enabled: Mapped[bool] = mapped_column(Boolean)


class NotificationSend(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only delivery log — one row per channel attempt. This is what
    makes deliverability debuggable (§8.12 point 4)."""

    __tablename__ = "notification_sends"
    __table_args__ = (
        Index("ix_notification_sends_tenant_notification", "tenant_id", "notification_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Nullable: a digest email batches many notifications into one send.
    notification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("notifications.id", ondelete="SET NULL")
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        _str_enum(NotificationChannel, "notification_channel")
    )
    status: Mapped[SendStatus] = mapped_column(_str_enum(SendStatus, "notification_send_status"))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(String(1000))
    sent_at: Mapped[datetime | None]


class NotificationDigestItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A digest-eligible notification parked for the batching sweep instead of
    being emailed immediately (quiet-hours / anti-3am-spam, §8.12 point 5).
    ``sent_at`` stamped once the sweep has folded it into a digest email —
    that stamp is the sweep's idempotency guard (same stance as
    ``listings.stale_flagged_at``)."""

    __tablename__ = "notification_digest_items"
    __table_args__ = (
        Index(
            "ix_notification_digest_items_tenant_user_pending",
            "tenant_id",
            "user_id",
            "sent_at",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    notification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE")
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        _str_enum(NotificationChannel, "notification_channel")
    )
    sent_at: Mapped[datetime | None]
