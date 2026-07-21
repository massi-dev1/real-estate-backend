"""Notification schemas (§8.12).

The ``/me`` surface is buyer/agent-facing: list your notifications, mark read,
read the unread count, and read/set your per-type channel preferences. No RBAC
permission — ownership is the authorization (same stance as favorites' ``/me``).
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.core.schema import InputSchema, OutSchema
from app.modules.notifications.models import NotificationChannel, NotificationType


class NotificationOut(OutSchema):
    id: uuid.UUID
    type: NotificationType
    payload: dict[str, Any]
    read_at: datetime | None
    created_at: datetime


class UnreadCountOut(OutSchema):
    unread: int


class MarkReadIn(InputSchema):
    """Mark a set of notifications read, or all of them. Both omitted is a no-op
    (idempotent)."""

    ids: list[uuid.UUID] | None = None
    all: bool = False


class WsTicketOut(OutSchema):
    """A short-lived ticket the client swaps for a WebSocket connection — §3:
    the auth material in the WS query string must not be a long-lived JWT."""

    ticket: str
    expires_in: int


class ChannelPreferenceOut(OutSchema):
    channel: NotificationChannel
    enabled: bool


class TypePreferenceOut(OutSchema):
    type: NotificationType
    digest_eligible: bool
    channels: list[ChannelPreferenceOut]


class PreferencesOut(OutSchema):
    types: list[TypePreferenceOut]


class ChannelPreferenceIn(InputSchema):
    channel: NotificationChannel
    enabled: bool


class TypePreferenceIn(InputSchema):
    type: NotificationType
    channels: list[ChannelPreferenceIn] = Field(min_length=1)


class PreferencesUpdateIn(InputSchema):
    """Partial: only the (type, channel) pairs named are written; everything
    else keeps its current explicit row or the type default."""

    types: list[TypePreferenceIn] = Field(min_length=1)
