"""Wire schemas for portal syndication (§8.14).

The admin surface configures which portals are enabled per tenant (stored in
``settings.syndication`` via the tenants boundary) and reads back per-listing
sync state. Portal keys are validated against the code-owned ``KNOWN_PORTALS``
allowlist — a tenant cannot name an adapter that doesn't exist.
"""

import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.core.schema import InputSchema, OutSchema
from app.integrations.portals.registry import KNOWN_PORTALS
from app.modules.syndication.models import PortalSyncState, SyncStatus


class PortalConfigIn(InputSchema):
    """One portal's per-tenant configuration."""

    enabled: bool = False
    base_url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)


class SyndicationSettingsIn(InputSchema):
    """Full replacement of a tenant's ``settings.syndication`` namespace: a map of
    portal key → config. Only known portal keys are accepted."""

    portals: dict[str, PortalConfigIn] = Field(default_factory=dict)

    @field_validator("portals")
    @classmethod
    def _known_portals_only(cls, value: dict[str, PortalConfigIn]) -> dict[str, PortalConfigIn]:
        unknown = sorted(set(value) - KNOWN_PORTALS)
        if unknown:
            raise ValueError(f"unknown portal keys: {unknown}")
        return value


class PortalConfigOut(OutSchema):
    """A portal's config as returned to the admin — the ``api_key`` is never
    echoed back (write-only secret), only whether one is set."""

    key: str
    enabled: bool
    base_url: str | None
    has_api_key: bool


class SyndicationSettingsOut(OutSchema):
    """Which portals exist (the allowlist) and how the tenant has each set up."""

    portals: list[PortalConfigOut]


class PortalSyncStateOut(OutSchema):
    id: uuid.UUID
    listing_id: uuid.UUID
    portal_key: str
    remote_id: str | None
    last_status: SyncStatus
    last_pushed_at: datetime | None
    last_error: str | None
    retry_count: int
    consecutive_failures: int
    circuit_open: bool
    updated_at: datetime

    @classmethod
    def from_state(cls, state: PortalSyncState) -> "PortalSyncStateOut":
        return cls.model_validate(state)
