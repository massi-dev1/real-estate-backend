"""Compliance module (§8.17) — consent records, cookie-consent config,
data-subject-request tracking. Two tenant-RLS tables (migration 0022).

``consent_records`` is **append-only proof**: what a subject consented to,
when, and the evidence (IP, user agent, timestamp) — plus a reference to the
exact versioned legal page consented to (Part 14's ``legal_pages.id`` +
version), never a duplicated copy of the policy text (§10.12). A subject is
identified by ``user_id`` (an account) *or* an ``email`` (an anonymous cookie /
saved-search opt-in) — one of the two, mirroring saved searches' owner-xor
model. Nothing is ever updated or deleted here; a withdrawal is a *new* record
with ``granted = false`` (the trail must show the whole history).

``cookie_consent_configs`` is one row per tenant: the category set
(necessary / analytics / marketing), tenant-editable copy, and the default
state. The analytics ingestion consent gate (§8.15, the TODO Part 21 left)
reads a *consent record*, not this config — this config drives the banner UI.

``dsr_requests`` tracks a data-subject request (export or erasure, §10.12)
through its lifecycle. An erasure request soft-deletes immediately and schedules
the 30-day purge; the sweep anonymizes/removes per data type and stamps the row.
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
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ConsentCategory(enum.StrEnum):
    """Cookie/consent categories (§8.17). ``necessary`` is always-on (no opt-out
    is meaningful for it) but is still recordable for a complete trail."""

    NECESSARY = "necessary"
    ANALYTICS = "analytics"
    MARKETING = "marketing"


class DsrKind(enum.StrEnum):
    EXPORT = "export"
    ERASURE = "erasure"


class DsrStatus(enum.StrEnum):
    PENDING = "pending"  # erasure: soft-deleted, awaiting the 30-day purge
    COMPLETED = "completed"  # export served, or erasure purge done
    CANCELLED = "cancelled"  # erasure withdrawn before the purge ran


def _str_enum(enum_cls: type[enum.StrEnum], name: str, length: int = 20) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class ConsentRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only consent proof. Never mutated — a withdrawal inserts a new
    row with ``granted = false`` so the full history is provable (§10.12)."""

    __tablename__ = "consent_records"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Exactly one identifies the subject (CHECK, like saved_searches' owner-xor).
    # ``user_id`` is SET NULL on account deletion — the *consent proof* must
    # survive the account it was tied to (an agency has to prove consent even
    # after erasure), so the record is de-linked, not cascade-deleted.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    category: Mapped[ConsentCategory] = mapped_column(
        _str_enum(ConsentCategory, "consent_category")
    )
    granted: Mapped[bool] = mapped_column(Boolean)
    # What was consented to. A cookie-banner accept/reject sets ``category``;
    # a legal-page acceptance additionally references the exact version.
    legal_page_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("legal_pages.id", ondelete="SET NULL")
    )
    legal_version: Mapped[int | None] = mapped_column(Integer)
    # Free-text label of what prompted the record (e.g. "cookie_banner",
    # "saved_search_signup", "privacy_policy") — the audit context.
    source: Mapped[str] = mapped_column(String(60))
    # Evidence (§10.12): who/where/when. ``created_at`` (TimestampMixin) is the
    # authoritative timestamp; these are the request-context proof.
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    # A cookie/session id when the subject is anonymous — ties a banner choice
    # to the session it applies to, so analytics ingestion can honour it.
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)

    __table_args__ = (
        # A consent record must identify its subject one way or another. Not a
        # strict XOR: a logged-in user acting in a browser session may carry
        # both a user_id and a session_id.
        CheckConstraint(
            "user_id IS NOT NULL OR email IS NOT NULL OR session_id IS NOT NULL",
            name="ck_consent_records_subject_present",
        ),
        Index("ix_consent_records_tenant_user", "tenant_id", "user_id"),
        Index("ix_consent_records_tenant_email", "tenant_id", "email"),
    )


class CookieConsentConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per tenant: the cookie-banner configuration (§8.17)."""

    __tablename__ = "cookie_consent_configs"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_cookie_consent_configs_tenant"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Ordered category descriptors: [{key, required, default_on, label:{i18n},
    # description:{i18n}}]. Envelope-validated (a known category key + a dict) —
    # the frontend owns the exact copy shape, like content page blocks.
    categories: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # Banner-level i18n copy: {title, body, accept_all, reject_all, ...}.
    banner_copy: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    # When enabled, the banner is shown; a tenant can disable it entirely.
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )


class DsrRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A data-subject request through its lifecycle (§10.12)."""

    __tablename__ = "dsr_requests"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # The requesting account. SET NULL: an erasure purge removes the account but
    # the DSR record (proof the request was honoured) survives, de-linked.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Denormalised so the record is meaningful after the user row is gone.
    subject_email: Mapped[str | None] = mapped_column(String(320))
    kind: Mapped[DsrKind] = mapped_column(_str_enum(DsrKind, "dsr_kind"))
    status: Mapped[DsrStatus] = mapped_column(
        _str_enum(DsrStatus, "dsr_status"),
        default=DsrStatus.PENDING,
        server_default=DsrStatus.PENDING.value,
    )
    # Erasure: when the 30-day purge is due. NULL for an export (served inline).
    purge_scheduled_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    # What the erasure sweep did, for the audit trail: {leads_anonymized: N,
    # deals_retained: N, ...}.
    result: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    ip: Mapped[str | None] = mapped_column(String(45))
