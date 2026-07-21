"""Reviews (§8.11) — moderated testimonials, tenant-owned and RLS-protected
(migration 0015).

A ``reviews`` row is a rating (1-5) + free text about an **agent**
(``agent_user_id`` — the profile owner's user id; NULL is a tenant-wide agency
testimonial) with an optional ``listing_id`` for context. Public submissions
land ``PENDING`` and pass through a ``PENDING → APPROVED | REJECTED`` moderation
queue; only ``APPROVED`` rows feed the public feed and the per-agent /
per-tenant aggregates (§8.5 "aggregation per agent and per tenant").

Reviews are testimonials, not leads — the author's name/email live on the row
rather than as a ``contacts`` record, keeping the module self-contained (no
cross-module model dependency).
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ReviewStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


def _str_enum(enum_cls: type[enum.StrEnum], name: str, length: int = 20) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class Review(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_reviews_rating_range"),
        # Moderation queue + public/aggregate reads: filter by status, newest
        # first, optionally per agent.
        Index(
            "ix_reviews_tenant_status_agent_created",
            "tenant_id",
            "status",
            "agent_user_id",
            "created_at",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # NULL = a tenant-wide agency testimonial, not tied to one agent.
    agent_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Context only; ON DELETE SET NULL so a removed listing doesn't drop the review.
    listing_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL"), index=True
    )
    rating: Mapped[int] = mapped_column(SmallInteger)
    title: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(4000))
    author_name: Mapped[str] = mapped_column(String(120))
    author_email: Mapped[str | None] = mapped_column(String(320))
    status: Mapped[ReviewStatus] = mapped_column(
        _str_enum(ReviewStatus, "review_status"),
        default=ReviewStatus.PENDING,
        server_default=ReviewStatus.PENDING.value,
    )
    # Set by the moderator on approval (or a future verified-client path).
    is_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    moderated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    moderated_at: Mapped[datetime | None]
    moderation_note: Mapped[str | None] = mapped_column(String(500))
