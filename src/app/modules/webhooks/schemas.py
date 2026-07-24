"""Webhook endpoint + delivery schemas (§8.14, §10.9).

``secret`` is write-only: it is returned exactly once, on creation (the tenant
must copy it to configure signature verification on their side), and never
echoed by any read afterwards — the same one-time-reveal stance as an API key.
The subscribed ``events`` are validated against the code-owned allowlist so a
tenant can't subscribe to an event that will never fire.
"""

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.core.events import EVENT_DEAL_CLOSED, EVENT_LEAD_CREATED, EVENT_LISTING_PUBLISHED
from app.core.schema import InputSchema, OutSchema

# The domain events a tenant may subscribe an endpoint to (§8.14). Code-owned —
# extending it is a one-line change here plus a producer that emits the event.
SUBSCRIBABLE_EVENTS = frozenset({EVENT_LEAD_CREATED, EVENT_LISTING_PUBLISHED, EVENT_DEAL_CLOSED})


class WebhookEndpointCreate(InputSchema):
    url: str = Field(max_length=2000)
    events: list[str] = Field(min_length=1)
    description: str | None = Field(default=None, max_length=200)

    @field_validator("events")
    @classmethod
    def _known_events(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - SUBSCRIBABLE_EVENTS)
        if unknown:
            raise ValueError(f"unknown event types: {unknown}")
        # De-duplicate while preserving determinism.
        return sorted(set(value))


class WebhookEndpointUpdate(InputSchema):
    url: str | None = Field(default=None, max_length=2000)
    events: list[str] | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None

    @field_validator("events")
    @classmethod
    def _known_events(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = sorted(set(value) - SUBSCRIBABLE_EVENTS)
        if unknown:
            raise ValueError(f"unknown event types: {unknown}")
        return sorted(set(value))


class WebhookEndpointOut(OutSchema):
    id: uuid.UUID
    url: str
    events: list[str]
    description: str | None
    is_active: bool
    circuit_open: bool
    last_error: str | None
    last_delivered_at: datetime | None
    created_at: datetime


class WebhookEndpointCreatedOut(WebhookEndpointOut):
    """Creation response only — carries the plaintext ``secret`` exactly once."""

    secret: str


class WebhookDeliveryOut(OutSchema):
    id: uuid.UUID
    endpoint_id: uuid.UUID
    event_type: str
    status: str
    attempts: int
    response_status: int | None
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime
