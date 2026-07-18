"""Appointments & tours (§8.7). Both tables are tenant-owned and RLS-protected
(migration 0011).

- ``agent_availability`` — per-agent schedule rows: weekly-template rows carry
  ``day_of_week``, one-off exception rows carry ``date`` (``is_block`` says
  whether the exception removes or adds a window). Times are interpreted in
  the tenant's ``settings.appointments.timezone`` (default UTC).
- ``appointments`` — the tour lifecycle
  ``requested → confirmed → completed | no_show``, cancellable from either
  live state. ``contact_id``/``lead_id`` link into the CRM by column only —
  the module talks to leads through its service, never its tables.
"""

import enum
import uuid
from datetime import date, datetime, time

from sqlalchemy import CheckConstraint, Enum, ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AppointmentStatus(enum.StrEnum):
    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


def _str_enum(enum_cls: type[enum.StrEnum], name: str, length: int = 20) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class AgentAvailability(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_availability"
    __table_args__ = (
        # A row is either weekly-template (day_of_week) or a dated exception —
        # never both, never neither.
        CheckConstraint(
            "(day_of_week IS NULL) != (date IS NULL)", name="weekly_xor_exception"
        ),
        CheckConstraint("start_time < end_time", name="start_before_end"),
        CheckConstraint(
            "day_of_week IS NULL OR (day_of_week BETWEEN 0 AND 6)", name="day_of_week_range"
        ),
        # Only dated exceptions can block; a blocking weekly row is just a
        # narrower template.
        CheckConstraint("NOT is_block OR date IS NOT NULL", name="block_is_exception"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    agent_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Python convention: Monday = 0 … Sunday = 6 (matches date.weekday()).
    day_of_week: Mapped[int | None]
    date: Mapped[date | None]
    start_time: Mapped[time]
    end_time: Mapped[time]
    is_block: Mapped[bool] = mapped_column(default=False, server_default=text("false"))


class Appointment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "appointments"
    __table_args__ = (CheckConstraint("start_at < end_at", name="start_before_end"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    agent_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    listing_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL")
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    # The CRM lead the booking minted — the no-show score penalty lands here.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    status: Mapped[AppointmentStatus] = mapped_column(
        _str_enum(AppointmentStatus, "appointment_status"),
        default=AppointmentStatus.REQUESTED,
        server_default=AppointmentStatus.REQUESTED.value,
    )
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    confirmed_at: Mapped[datetime | None]
    # Idempotency stamps for the Beat reminder sweep (same stance as
    # listings.stale_flagged_at): a reminder is sent at most once per window.
    reminder_24h_sent_at: Mapped[datetime | None]
    reminder_1h_sent_at: Mapped[datetime | None]
