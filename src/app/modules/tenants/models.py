"""Tenants and their domains — global (platform-level) tables, no RLS (§4.3).

The tenant-resolution middleware must query these *before* any tenant context
exists, so they are deliberately outside row-level security. Part 22 (§8.16)
adds the billing/admin machinery — subscriptions, usage counters, the webhook
idempotency log, and the audit trail — all likewise platform-scoped.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    ForeignKey,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TenantStatus(enum.StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class DomainVerificationStatus(enum.StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"


class SubscriptionStatus(enum.StrEnum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"


def _status_column(enum_cls: type[enum.StrEnum], name: str, length: int) -> Any:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    status: Mapped[TenantStatus] = mapped_column(
        _status_column(TenantStatus, "tenant_status", 20),
        default=TenantStatus.TRIAL,
        server_default=TenantStatus.TRIAL.value,
    )
    # Quota tier (§8.16). The plan → limits table lives in code (app.modules.
    # tenants.plans), not the DB — auditable in git, like the RBAC matrix.
    plan: Mapped[str] = mapped_column(String(40), default="trial", server_default="trial")
    # Trial expiry (§8.16): the trial-expiry sweep suspends a trial past this.
    trial_ends_at: Mapped[datetime | None]
    # Offboard lifecycle (§8.16): export requested → deletion scheduled → purged.
    offboarding_at: Mapped[datetime | None]
    deletion_scheduled_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
    # Private-bucket key of the offboard export archive (§8.16).
    export_object_key: Mapped[str | None] = mapped_column(String(500))
    # Branding, locales, currency, feature toggles — validated shape comes with
    # the site-config work (§4.4); stored as JSONB from day one.
    settings: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )

    domains: Mapped[list["TenantDomain"]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
        order_by="TenantDomain.created_at",
    )


class TenantDomain(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenant_domains"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    domain: Mapped[str] = mapped_column(String(253), unique=True)
    is_primary: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    # DNS TXT-record challenge (§8.16): the verification value the tenant must
    # publish, and the resulting status. ``verified_at`` is what ops-side
    # on-demand-TLS wiring keys on to provision a certificate.
    verification_token: Mapped[str | None] = mapped_column(String(64))
    verification_status: Mapped[DomainVerificationStatus] = mapped_column(
        _status_column(DomainVerificationStatus, "domain_verification_status", 20),
        default=DomainVerificationStatus.PENDING,
        server_default=DomainVerificationStatus.PENDING.value,
    )
    verified_at: Mapped[datetime | None]

    tenant: Mapped[Tenant] = relationship(back_populates="domains")


class TenantUsage(TimestampMixin, Base):
    """One row per tenant holding running quota counters (§8.16).

    Incremented at write-time by the owning services (a listing create bumps
    ``listings_count``, a media confirm bumps ``storage_bytes``), so a quota
    check is an O(1) read rather than a cross-module recompute scan. The row's
    PK *is* ``tenant_id`` (one-to-one with the tenant).
    """

    __tablename__ = "tenant_usage"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    listings_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    agents_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    storage_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    emails_sent: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # The "YYYY-MM" the email counter belongs to; a new month resets it.
    emails_period: Mapped[str | None] = mapped_column(String(7))


class TenantSubscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Billing subscription mirror (§8.16). The provider (Stripe/Chargily) is
    the source of truth; this row is what the app reads to gate access. One
    live subscription per tenant (v1)."""

    __tablename__ = "tenant_subscriptions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), unique=True
    )
    provider: Mapped[str] = mapped_column(String(40))
    plan: Mapped[str] = mapped_column(String(40))
    status: Mapped[SubscriptionStatus] = mapped_column(
        _status_column(SubscriptionStatus, "subscription_status", 20)
    )
    provider_customer_id: Mapped[str | None] = mapped_column(String(255))
    provider_subscription_id: Mapped[str | None] = mapped_column(String(255), index=True)
    current_period_end: Mapped[datetime | None]
    # Dunning grace window: a past_due subscription stays reachable until this
    # instant; the dunning sweep suspends the tenant once it passes (§8.16).
    grace_until: Mapped[datetime | None]
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )


class BillingEvent(UUIDPrimaryKeyMixin, Base):
    """Append-only webhook idempotency log (§10.9). A provider event is
    processed at most once — the unique ``(provider, event_id)`` makes a
    replayed webhook a no-op."""

    __tablename__ = "billing_events"

    provider: Mapped[str] = mapped_column(String(40))
    event_id: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(120))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    received_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class AuditLogEntry(UUIDPrimaryKeyMixin, Base):
    """Minimal append-only audit trail (§10.11). Added here for the one use
    Part 22 requires — impersonation start/stop — plus tenant-lifecycle admin
    actions; Part 23/compliance broadens the write sites and the reporting."""

    __tablename__ = "audit_log"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL")
    )
    actor_user_id: Mapped[uuid.UUID | None]
    actor_role: Mapped[str | None] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str | None] = mapped_column(String(255))
    audit_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    ip: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
