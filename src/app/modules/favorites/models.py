"""Favorites & saved searches (§8.9).

Both tables are strictly tenant-owned and RLS-protected (migration 0009).
``saved_searches`` rows belong either to an account (``user_id``) or to a
bare email address (anonymous alert signup, double-opt-in) — exactly one of
the two, enforced by a CHECK constraint.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AlertFrequency(enum.StrEnum):
    INSTANT = "instant"
    DAILY = "daily"
    WEEKLY = "weekly"


class Favorite(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "favorites"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "listing_id"),
        # The buyer-dashboard list: newest favorite first, per user.
        Index("ix_favorites_tenant_user_created", "tenant_id", "user_id", "created_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )


class SavedSearch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "saved_searches"
    __table_args__ = (
        # Exactly one owner: an account or an anonymous email — never both,
        # never neither.
        CheckConstraint("(user_id IS NULL) <> (email IS NULL)", name="owner_xor_email"),
        # The alert matchers' scan: active rows of one frequency per tenant.
        Index("ix_saved_searches_tenant_active_freq", "tenant_id", "is_active", "frequency"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Lowercased at the write boundary (schema validator), like contacts.
    email: Mapped[str | None] = mapped_column(String(320))
    name: Mapped[str] = mapped_column(String(120))
    # The validated camelCase dump of PublicListingFilters (§8.3) — replayable
    # against the public search verbatim.
    filters: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    # The locale the search was created under — FTS `q` terms must be parsed
    # with the same text-search-config family they were written in (§8.3), and
    # it doubles as the alert email's content locale.
    locale: Mapped[str] = mapped_column(String(10), default="fr", server_default="fr")
    frequency: Mapped[AlertFrequency] = mapped_column(
        Enum(
            AlertFrequency,
            name="alert_frequency",
            native_enum=False,
            length=20,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=AlertFrequency.INSTANT,
        server_default=AlertFrequency.INSTANT.value,
    )
    # Digest watermark: only listings published after this are "new".
    last_run_at: Mapped[datetime | None]
    # Anonymous signups start inactive until the double-opt-in confirm (§8.9).
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
