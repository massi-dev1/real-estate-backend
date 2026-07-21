"""Leads & CRM (§8.4). One module covers contacts, leads, activities,
assignment policy and drip state — a deliberate deviation from project.md §5's
literal ``leads``/``clients`` split (see leads/service.py docstring): every
lead has a mandatory ``contact_id`` and no standalone contact-portal lifecycle
exists yet, so splitting the module today would be ceremony without an
isolation benefit.

All five tables are strictly tenant-owned and RLS-protected (migration 0007).
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Enum, ForeignKey, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class LeadSource(enum.StrEnum):
    LISTING_FORM = "listing_form"
    VALUATION = "valuation"
    MORTGAGE = "mortgage"
    MARKET_REPORT = "market_report"
    SEARCH_SIGNUP = "search_signup"
    CHAT = "chat"
    WHATSAPP_CLICK = "whatsapp_click"
    TOUR_REQUEST = "tour_request"
    PHONE = "phone"
    PORTAL = "portal"
    AD = "ad"
    OTHER = "other"


class LeadStage(enum.StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    TOURING = "touring"
    OFFER = "offer"
    WON = "won"
    LOST = "lost"


class ActivityType(enum.StrEnum):
    NOTE = "note"
    CALL = "call"
    EMAIL = "email"
    SMS = "sms"
    STATUS_CHANGE = "status_change"
    ASSIGNMENT = "assignment"
    TOUR = "tour"
    NO_SHOW = "no_show"
    SYSTEM = "system"


class AssignmentStrategy(enum.StrEnum):
    LISTING_AGENT = "listing_agent"
    ROUND_ROBIN = "round_robin"
    TERRITORY = "territory"


class DripStopReason(enum.StrEnum):
    STAGE_ADVANCED = "stage_advanced"
    REPLIED = "replied"
    SEQUENCE_COMPLETE = "sequence_complete"
    MANUAL = "manual"


def _str_enum(enum_cls: type[enum.StrEnum], name: str, length: int = 20) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class Contact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contacts"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    first_name: Mapped[str | None] = mapped_column(String(80))
    last_name: Mapped[str | None] = mapped_column(String(80))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(32))
    whatsapp: Mapped[str | None] = mapped_column(String(32))
    # {marketing_email, sms, ts, proof} — §6.4.
    consent: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    notes: Mapped[str | None] = mapped_column(Text)


class Lead(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leads"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    listing_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL"), index=True
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    source: Mapped[LeadSource] = mapped_column(_str_enum(LeadSource, "lead_source"))
    # {utm_source, utm_medium, utm_campaign, page, referrer} — §8.4.
    source_meta: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    stage: Mapped[LeadStage] = mapped_column(
        _str_enum(LeadStage, "lead_stage"),
        default=LeadStage.NEW,
        server_default=LeadStage.NEW.value,
    )
    score: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    lost_reason: Mapped[str | None] = mapped_column(String(200))
    first_response_at: Mapped[datetime | None]


class LeadActivity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only unified timeline — also the "Contact timeline" data source
    (§8.4), joined across a contact's leads at the service layer."""

    __tablename__ = "lead_activities"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    # NULL for system-generated rows (capture, auto-assignment).
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    type: Mapped[ActivityType] = mapped_column(_str_enum(ActivityType, "lead_activity_type"))
    payload: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )


class AssignmentRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per tenant — the assignment *policy*, not per-lead state."""

    __tablename__ = "assignment_rules"
    __table_args__ = (UniqueConstraint("tenant_id"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    strategy: Mapped[AssignmentStrategy] = mapped_column(
        _str_enum(AssignmentStrategy, "assignment_strategy"),
        default=AssignmentStrategy.LISTING_AGENT,
        server_default=AssignmentStrategy.LISTING_AGENT.value,
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )


class LeadDripState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per lead — where a drip sequence is in its steps."""

    __tablename__ = "lead_drip_state"
    __table_args__ = (UniqueConstraint("lead_id"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"))
    step_index: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    next_send_at: Mapped[datetime]
    stopped_at: Mapped[datetime | None]
    stopped_reason: Mapped[DripStopReason | None] = mapped_column(
        _str_enum(DripStopReason, "drip_stop_reason")
    )
