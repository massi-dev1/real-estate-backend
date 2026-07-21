"""Transactions & deals (§8.13). All three tables are tenant-owned and
RLS-protected (migration 0018).

- ``Deal`` — the back-office deal record once a lead converts:
  ``open → under_contract → closed_won | closed_lost``. ``listing_id`` /
  ``lead_id`` / ``contact_id`` link into the other modules **by column only**
  — the module talks to listings/leads through their services, never their
  tables. Money follows §9 (``price``/``commission_amount`` are ``Numeric``
  with a sibling ``currency``, mirroring listings' ``price``).
- ``DealMilestone`` — checklist items (title, due_date, owner, completed_at),
  template-seeded on create or added ad hoc.
- ``DealDocument`` — a private-bucket object key + server-computed sha256, plus
  the e-signature *seam* columns (adapter designed, provider deferred).
"""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DealStatus(enum.StrEnum):
    OPEN = "open"
    UNDER_CONTRACT = "under_contract"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"


# Terminal states — a closed deal no longer moves through the pipeline.
CLOSED_STATUSES = frozenset({DealStatus.CLOSED_WON, DealStatus.CLOSED_LOST})


class CommissionBasis(enum.StrEnum):
    PERCENTAGE = "percentage"  # commission_amount = price * commission_rate / 100
    FLAT = "flat"  # commission_amount is the figure directly


class DealDocumentStatus(enum.StrEnum):
    PENDING = "pending"  # presigned, upload not yet confirmed
    READY = "ready"  # object present, sha256 computed
    FAILED = "failed"  # confirm found no object


class SignatureStatus(enum.StrEnum):
    NONE = "none"
    REQUESTED = "requested"
    SIGNED = "signed"
    DECLINED = "declined"


def _str_enum(enum_cls: type[enum.StrEnum], name: str, length: int = 40) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class Deal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "deals"
    __table_args__ = (
        CheckConstraint(
            "commission_rate IS NULL OR (commission_rate >= 0 AND commission_rate <= 100)",
            name="ck_deals_commission_rate_range",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # The owning agent — visibility scoping (§8.5) keys on this.
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[DealStatus] = mapped_column(
        _str_enum(DealStatus, "deal_status"),
        default=DealStatus.OPEN,
        server_default=DealStatus.OPEN.value,
    )
    # Column-only links into the other modules.
    listing_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="DZD", server_default="DZD")
    commission_basis: Mapped[CommissionBasis | None] = mapped_column(
        _str_enum(CommissionBasis, "commission_basis")
    )
    commission_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    commission_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    closed_at: Mapped[datetime | None]
    lost_reason: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)


class DealMilestone(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "deal_milestones"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(255))
    due_date: Mapped[date | None]
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    completed_at: Mapped[datetime | None]
    position: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Idempotency stamp for the due-milestone reminder sweep (same stance as
    # appointments.reminder_*_sent_at): each milestone is reminded at most once.
    reminder_sent_at: Mapped[datetime | None]


class DealDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "deal_documents"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), index=True
    )
    doc_type: Mapped[str] = mapped_column(String(60))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120))
    storage_key: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # Server-computed on confirm (never trust a client claim — Part 6 stance).
    sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[DealDocumentStatus] = mapped_column(
        _str_enum(DealDocumentStatus, "deal_document_status"),
        default=DealDocumentStatus.PENDING,
        server_default=DealDocumentStatus.PENDING.value,
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # E-signature seam: columns present, adapter interface designed, provider
    # integration deferred (§8.13 — "design the seam, defer the provider").
    signature_status: Mapped[SignatureStatus] = mapped_column(
        _str_enum(SignatureStatus, "deal_signature_status"),
        default=SignatureStatus.NONE,
        server_default=SignatureStatus.NONE.value,
    )
    signature_request_id: Mapped[str | None] = mapped_column(String(255))
