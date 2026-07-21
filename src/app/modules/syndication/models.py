"""Portal syndication sync-state (§8.14, migration 0019).

One tenant-owned, RLS-protected table. ``PortalSyncState`` is the per-listing,
per-portal record of what we last pushed and how it went — the source of truth
for the portal admin's sync-state view and for the circuit breaker.

The listing/portal pairing is the natural key (``uq_portal_sync_listing_portal``).
``listing_id`` is a column-only link into the listings module (FK for integrity +
``ON DELETE CASCADE`` so a purged listing's sync rows go with it), but the
syndication service never imports listings' models — it reaches listings through
their service boundary.

Circuit breaker (§8.14): ``consecutive_failures`` counts unbroken failures;
once it crosses a threshold the service flips ``circuit_open`` so the sync task
stops even trying that portal-tenant pair (no retry-storm) until an admin
re-enables it. A success resets both.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SyncStatus(enum.StrEnum):
    PENDING = "pending"  # enqueued, not yet attempted (or re-queued)
    SYNCED = "synced"  # last push/update succeeded
    REMOVED = "removed"  # last remove succeeded — no longer on the portal
    FAILED = "failed"  # last attempt failed (may retry)
    PAUSED = "paused"  # circuit open — syncing suspended for this pair


def _str_enum(enum_cls: type[enum.StrEnum], name: str, length: int = 20) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class PortalSyncState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "portal_sync_state"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "listing_id", "portal_key", name="uq_portal_sync_listing_portal"
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Column-only link into listings (no cross-module model import).
    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    portal_key: Mapped[str] = mapped_column(String(40))
    # The portal's own id for this listing, set on the first successful push.
    remote_id: Mapped[str | None] = mapped_column(String(255))
    last_status: Mapped[SyncStatus] = mapped_column(
        _str_enum(SyncStatus, "portal_sync_status"),
        default=SyncStatus.PENDING,
        server_default=SyncStatus.PENDING.value,
    )
    last_pushed_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Circuit breaker: unbroken failures, and whether the breaker is open.
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    circuit_open: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
