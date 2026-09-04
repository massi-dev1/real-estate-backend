"""Pydantic schemas for appointments & tours (§8.7).

Public booking reuses the leads module's ``_CaptureBase`` (honeypot +
``renderedAt`` + contact) so a tour request carries exactly the same spam
defense as every other capture surface. Portal schemas follow the usual
explicit ``*Out`` shapes.
"""

import datetime as dt
import uuid
from datetime import datetime, time
from typing import Self

from pydantic import Field, model_validator

from app.core.schema import InputSchema, OutSchema
from app.modules.appointments.models import AppointmentStatus
from app.modules.leads.schemas import _CaptureBase

# How far ahead the public slot search (and booking) reaches.
MAX_BOOKING_DAYS_AHEAD = 90
# Full replacement PUT keeps the schedule bounded per agent.
MAX_AVAILABILITY_RULES = 100


class AvailabilityRuleIn(InputSchema):
    """One schedule row: weekly template (``dayOfWeek``) or dated exception
    (``date``); a blocking exception removes matching template time."""

    # `dt.date`, not a bare `date` import: a field named after its own type
    # would bind `date = None` in the class body before the annotation is read.
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    date: dt.date | None = None
    start_time: time
    end_time: time
    is_block: bool = False

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.day_of_week is None) == (self.date is None):
            raise ValueError("provide exactly one of dayOfWeek or date")
        if self.start_time >= self.end_time:
            raise ValueError("startTime must be before endTime")
        if self.is_block and self.date is None:
            raise ValueError("only dated exceptions can block")
        return self


class AvailabilityPut(InputSchema):
    rules: list[AvailabilityRuleIn] = Field(max_length=MAX_AVAILABILITY_RULES)


class AvailabilityRuleOut(OutSchema):
    id: uuid.UUID
    day_of_week: int | None
    date: dt.date | None
    start_time: time
    end_time: time
    is_block: bool


class SlotOut(OutSchema):
    start_at: datetime
    end_at: datetime


class TourBookingCreate(_CaptureBase):
    """Public tour request: a slot start (UTC instant, must match a free slot
    exactly) plus the shared capture shape — source is fixed server-side."""

    start_at: datetime


class TourBookingOut(OutSchema):
    id: uuid.UUID
    status: AppointmentStatus
    start_at: datetime
    end_at: datetime


class AppointmentOut(OutSchema):
    id: uuid.UUID
    agent_user_id: uuid.UUID
    listing_id: uuid.UUID | None
    contact_id: uuid.UUID
    lead_id: uuid.UUID | None
    status: AppointmentStatus
    start_at: datetime
    end_at: datetime
    confirmed_at: datetime | None
    reminder_24h_sent_at: datetime | None
    reminder_1h_sent_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MyAppointmentOut(OutSchema):
    """A visitor's own tour, for ``/me/appointments``.

    Deliberately a narrower shape than the portal's ``AppointmentOut``: the
    reminder-dispatch stamps and ``contactId`` are internal bookkeeping, and
    ``leadId`` exposes that the visitor is a tracked CRM record — none of it is
    the visitor's business, so none of it is on the wire.
    """

    id: uuid.UUID
    agent_user_id: uuid.UUID
    listing_id: uuid.UUID | None
    status: AppointmentStatus
    start_at: datetime
    end_at: datetime
    confirmed_at: datetime | None
    created_at: datetime


class AppointmentTransitionRequest(InputSchema):
    to_status: AppointmentStatus

    @model_validator(mode="after")
    def not_requested(self) -> Self:
        if self.to_status is AppointmentStatus.REQUESTED:
            raise ValueError("an appointment cannot move back to requested")
        return self


class IcalUrlOut(OutSchema):
    url: str
