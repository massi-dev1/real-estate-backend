"""Pydantic schemas for the leads module (§8.4).

Public capture accepts anonymous submissions from lead-gen forms — its output
is deliberately minimal (an id only) so an unauthenticated caller learns
nothing about stage/score/assignment. Portal schemas mirror listings' shapes:
explicit ``*Out`` models, ``exclude_unset`` PATCH semantics.
"""

import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from app.core.schema import BaseSchema, InputSchema, OutSchema
from app.modules.leads.models import ActivityType, AssignmentStrategy, LeadSource, LeadStage

# Activity types a client may log directly; the rest are system-generated
# (capture, auto-assignment, stage transitions).
CLIENT_ACTIVITY_TYPES: frozenset[ActivityType] = frozenset(
    {ActivityType.NOTE, ActivityType.CALL, ActivityType.EMAIL, ActivityType.SMS, ActivityType.TOUR}
)

# A capture submitted faster than this many seconds after the form rendered
# is treated as a bot (§10.8 baseline defense).
MIN_FILL_SECONDS = 3
# ...and one claiming to have been rendered longer ago than this is a stale
# or replayed form (rendered_at is client-supplied, so without an upper bound
# a bot just backdates it and the minimum-fill check never fires).
MAX_FORM_AGE_SECONDS = 24 * 3600


class ContactCaptureIn(InputSchema):
    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=32)
    whatsapp: str | None = Field(default=None, max_length=32)
    marketing_consent: bool = False

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return value.strip().lower() or None if value else None

    @field_validator("phone", "whatsapp")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None

    @model_validator(mode="after")
    def email_or_phone(self) -> Self:
        if not self.email and not self.phone:
            raise ValueError("either email or phone is required")
        return self


class LeadCaptureCreate(InputSchema):
    contact: ContactCaptureIn
    listing_id: uuid.UUID | None = None
    source: LeadSource
    message: str | None = Field(default=None, max_length=2000)
    utm_source: str | None = Field(default=None, max_length=100)
    utm_medium: str | None = Field(default=None, max_length=100)
    utm_campaign: str | None = Field(default=None, max_length=100)
    page: str | None = Field(default=None, max_length=500)
    referrer: str | None = Field(default=None, max_length=500)
    # Spam defense (§10.8): a hidden field real browsers never fill, and the
    # timestamp the form was rendered at, so instant (headless) submits fail
    # a minimum-fill-time check. See leads/service.py::capture_lead for how
    # the two failure modes differ (silent drop vs. visible 422).
    hp: str = ""
    rendered_at: datetime

    @model_validator(mode="after")
    def not_too_fast(self) -> Self:
        now = datetime.now(self.rendered_at.tzinfo) if self.rendered_at.tzinfo else datetime.now()
        elapsed = (now - self.rendered_at).total_seconds()
        if elapsed < MIN_FILL_SECONDS:
            raise ValueError("form submitted too quickly")
        if elapsed > MAX_FORM_AGE_SECONDS:
            raise ValueError("form is stale — please reload and resubmit")
        return self


class LeadCaptureOut(OutSchema):
    id: uuid.UUID


class ContactOut(OutSchema):
    id: uuid.UUID
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    whatsapp: str | None
    consent: dict[str, Any]
    tags: list[str]
    notes: str | None
    created_at: datetime
    updated_at: datetime


class ContactUpdate(InputSchema):
    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=32)
    whatsapp: str | None = Field(default=None, max_length=32)
    consent: dict[str, Any] | None = None
    tags: list[str] | None = None
    notes: str | None = None

    # Same normalization as capture — the stored email must stay lowercase so
    # every write path agrees with the lower(email) dedupe index.
    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return value.strip().lower() or None if value else None

    @field_validator("phone", "whatsapp")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None


class LeadOut(OutSchema):
    id: uuid.UUID
    contact_id: uuid.UUID
    listing_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    source: LeadSource
    source_meta: dict[str, Any]
    stage: LeadStage
    score: int
    lost_reason: str | None
    first_response_at: datetime | None
    created_at: datetime
    updated_at: datetime


class LeadDetailOut(LeadOut):
    contact: ContactOut


class LeadCreate(InputSchema):
    """Manual lead entry (e.g. logging a phone-in lead) — exactly one of
    ``contact_id`` (existing contact) or ``contact`` (inline capture) must be
    given."""

    contact_id: uuid.UUID | None = None
    contact: ContactCaptureIn | None = None
    listing_id: uuid.UUID | None = None
    source: LeadSource
    agent_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def exactly_one_contact_source(self) -> Self:
        if (self.contact_id is None) == (self.contact is None):
            raise ValueError("provide exactly one of contactId or contact")
        return self


class LeadUpdate(InputSchema):
    """Reassignment / listing-link edits only — stage moves through the
    dedicated transition endpoint, not this PATCH."""

    agent_id: uuid.UUID | None = None
    listing_id: uuid.UUID | None = None


class StageTransitionRequest(InputSchema):
    to_stage: LeadStage
    lost_reason: str | None = Field(default=None, max_length=200)


class LeadFilters(BaseSchema):
    stage: LeadStage | None = None
    agent_id: uuid.UUID | None = None
    source: LeadSource | None = None
    listing_id: uuid.UUID | None = None


class ActivityCreate(InputSchema):
    type: ActivityType
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def client_type_only(cls, value: ActivityType) -> ActivityType:
        if value not in CLIENT_ACTIVITY_TYPES:
            raise ValueError(f"activity type '{value.value}' cannot be logged directly")
        return value


class ActivityOut(OutSchema):
    id: uuid.UUID
    lead_id: uuid.UUID
    actor_id: uuid.UUID | None
    type: ActivityType
    payload: dict[str, Any]
    created_at: datetime


class AssignmentRuleConfig(InputSchema):
    """Typed write-boundary schema for the rule's JSONB ``config`` blob — a
    free-form dict here would let one bad value (a non-UUID in ``agent_pool``)
    500 every subsequent public capture when the round-robin engine parses it
    (review finding)."""

    agent_pool: list[uuid.UUID] | None = None
    max_open_leads_per_agent: int | None = Field(default=None, ge=1)


class AssignmentRuleUpdate(InputSchema):
    strategy: AssignmentStrategy
    config: AssignmentRuleConfig = Field(default_factory=AssignmentRuleConfig)


class AssignmentRuleOut(OutSchema):
    id: uuid.UUID
    strategy: AssignmentStrategy
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class TimelineEntryOut(OutSchema):
    kind: Literal["lead_created", "activity"]
    at: datetime
    lead_id: uuid.UUID
    activity: ActivityOut | None = None
    lead_stage: LeadStage | None = None


class ContactTimelineOut(OutSchema):
    contact: ContactOut
    leads: list[LeadOut]
    entries: list[TimelineEntryOut]
