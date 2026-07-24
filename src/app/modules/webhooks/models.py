"""Outbound webhook endpoints + delivery log (§8.14, §10.9, migration 0024).

Two tenant-owned, RLS-protected tables:

- ``webhook_endpoints`` — a tenant-registered URL the platform POSTs signed
  domain events to, with the HMAC secret, the subscribed event types, and the
  same **circuit-breaker** columns ``portal_sync_state`` uses (§8.14): after
  ``CIRCUIT_BREAKER_THRESHOLD`` consecutive failures the breaker opens and
  delivery stops until it is re-enabled, so one dead receiver can't retry-storm.
- ``webhook_deliveries`` — the append-only per-attempt log (§10.9): which event
  went to which endpoint, the response status, and the outcome. A delivery is
  created ``pending`` and updated in place across retries.

``secret`` is stored as-is: it is *our* signing key for *this* endpoint (like a
portal ``api_key``), symmetric by design — the receiver holds the same value to
verify. It is never echoed back on the wire (write-only in the schemas).
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _str_enum(enum_cls: type[enum.StrEnum], name: str, length: int = 20) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class DeliveryStatus(enum.StrEnum):
    PENDING = "pending"  # created, not yet attempted (or mid-retry)
    DELIVERED = "delivered"  # receiver returned 2xx — terminal
    FAILED = "failed"  # exhausted retries or a permanent rejection — terminal


class WebhookEndpoint(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "webhook_endpoints"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(String(2000))
    # Our HMAC-SHA256 signing secret for this endpoint (write-only on the wire).
    secret: Mapped[str] = mapped_column(String(255))
    # Which domain events this endpoint receives (event-name strings from
    # core.events). Stored as a JSONB list, filtered with the `@>` containment
    # pattern the codebase uses for listings.features / blog.tags.
    events: Mapped[list[str]] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    description: Mapped[str | None] = mapped_column(String(200))
    # Circuit breaker (§8.14) — same shape as portal_sync_state.
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    circuit_open: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_delivered_at: Mapped[datetime | None]


class WebhookDelivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One event → one endpoint delivery attempt record (§10.9)."""

    __tablename__ = "webhook_deliveries"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(80))
    # The exact signed body sent (kept for replay/debugging, §10.9).
    payload: Mapped[dict[str, Any]]
    status: Mapped[DeliveryStatus] = mapped_column(
        _str_enum(DeliveryStatus, "webhook_delivery_status"),
        default=DeliveryStatus.PENDING,
        server_default=DeliveryStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    response_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None]
