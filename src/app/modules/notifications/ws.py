"""WebSocket ticket helpers + the preferences response builder.

The WS ticket is a single-use, short-lived opaque token minted over HTTPS and
redeemed at socket-open time (§3: don't put a long-lived JWT in the WS query
string). It maps to ``tenant_id:user_id`` in Redis with a tight TTL and is
consumed with ``GETDEL`` so a captured ticket can't be replayed.
"""

import secrets
import uuid

from redis.asyncio import Redis

from app.modules.notifications.models import NotificationChannel, NotificationType
from app.modules.notifications.schemas import (
    ChannelPreferenceOut,
    PreferencesOut,
    TypePreferenceOut,
)
from app.modules.notifications.types import definition_for

WS_TICKET_TTL_SECONDS = 60


def _ticket_key(ticket: str) -> str:
    return f"notify:ws-ticket:{ticket}"


async def mint_ws_ticket(redis: Redis, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    ticket = secrets.token_urlsafe(32)
    await redis.set(_ticket_key(ticket), f"{tenant_id}:{user_id}", ex=WS_TICKET_TTL_SECONDS)
    return ticket


async def redeem_ws_ticket(redis: Redis, ticket: str, *, tenant_id: uuid.UUID) -> uuid.UUID | None:
    """Single-use: consume the ticket and return its user id iff it was minted
    for this tenant. ``GETDEL`` makes it non-replayable."""
    raw = await redis.getdel(_ticket_key(ticket))
    if not raw:
        return None
    value = raw.decode() if isinstance(raw, bytes | bytearray) else str(raw)
    stored_tenant, _, stored_user = value.partition(":")
    if stored_tenant != str(tenant_id):
        return None
    try:
        return uuid.UUID(stored_user)
    except ValueError:
        return None


def preferences_out(
    matrix: dict[NotificationType, dict[NotificationChannel, bool]],
) -> PreferencesOut:
    return PreferencesOut(
        types=[
            TypePreferenceOut(
                type=type_,
                digest_eligible=definition_for(type_).digest_eligible,
                channels=[
                    ChannelPreferenceOut(channel=channel, enabled=enabled)
                    for channel, enabled in channels.items()
                ],
            )
            for type_, channels in matrix.items()
        ]
    )
