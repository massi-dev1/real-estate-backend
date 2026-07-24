"""HTTP + WebSocket layer for notifications (§8.12).

- ``me_router`` (``/me/...``) — the buyer/agent surface: list notifications,
  mark read, unread count, WS ticket, read/set preferences. Ownership is the
  authorization (no RBAC permission), same stance as favorites' ``/me``.
- ``ws_router`` (``/ws/notifications``) — the live push channel. Authenticated
  by a **short-lived ticket** obtained over HTTPS from ``/me/notifications/
  ws-ticket`` and passed in the query string, *not* a long-lived JWT (§3). The
  connection subscribes to the user's Redis channel and relays each published
  event as a JSON text frame.
"""

import uuid

from fastapi import APIRouter, Query, Request, WebSocket, status
from redis.asyncio import Redis

from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import CurrentUserDep
from app.core.tenancy import TenantContext, TenantDep, TenantResolver
from app.modules.notifications.schemas import (
    MarkReadIn,
    NotificationOut,
    PreferencesOut,
    PreferencesUpdateIn,
    UnreadCountOut,
    WsTicketOut,
)
from app.modules.notifications.service import NotificationsServiceDep, user_channel
from app.modules.notifications.ws import (
    WS_TICKET_TTL_SECONDS,
    mint_ws_ticket,
    preferences_out,
    redeem_ws_ticket,
)

me_router = APIRouter(prefix="/me/notifications", tags=["notifications:me"])


@me_router.get("")
async def list_notifications(
    tenant: TenantDep,
    service: NotificationsServiceDep,
    actor: CurrentUserDep,
    unread_only: bool = Query(default=False, alias="unreadOnly"),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[NotificationOut]:
    items, next_cursor = await service.list_for_user(
        tenant, actor.id, unread_only=unread_only, cursor=cursor, limit=limit
    )
    return Page(
        items=[NotificationOut.model_validate(n) for n in items],
        next_cursor=next_cursor,
    )


@me_router.get("/unread-count")
async def unread_count(
    tenant: TenantDep, service: NotificationsServiceDep, actor: CurrentUserDep
) -> UnreadCountOut:
    return UnreadCountOut(unread=await service.unread_count(tenant, actor.id))


@me_router.post("/mark-read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(
    data: MarkReadIn,
    tenant: TenantDep,
    service: NotificationsServiceDep,
    actor: CurrentUserDep,
) -> None:
    await service.mark_read(tenant, actor.id, ids=data.ids, all_=data.all)


@me_router.post("/ws-ticket")
async def create_ws_ticket(
    request: Request, tenant: TenantDep, actor: CurrentUserDep
) -> WsTicketOut:
    redis: Redis = request.app.state.redis
    ticket = await mint_ws_ticket(redis, tenant_id=tenant.id, user_id=actor.id)
    return WsTicketOut(ticket=ticket, expires_in=WS_TICKET_TTL_SECONDS)


@me_router.get("/preferences")
async def get_preferences(
    tenant: TenantDep, service: NotificationsServiceDep, actor: CurrentUserDep
) -> PreferencesOut:
    matrix = await service.get_preferences(tenant, actor.id)
    return preferences_out(matrix)


@me_router.put("/preferences")
async def update_preferences(
    data: PreferencesUpdateIn,
    tenant: TenantDep,
    service: NotificationsServiceDep,
    actor: CurrentUserDep,
) -> PreferencesOut:
    matrix = await service.update_preferences(tenant, actor.id, data)
    return preferences_out(matrix)


ws_router = APIRouter(tags=["notifications:ws"])


@ws_router.websocket("/ws/notifications")
async def notifications_ws(websocket: WebSocket, ticket: str = Query(...)) -> None:
    """Live notification stream for one authenticated user.

    The tenant is resolved from the Host header (the ASGI tenant middleware only
    runs for HTTP scopes, so WS resolves it here), then the ticket is redeemed
    against that exact tenant + a user — a ticket minted for agency A can't open
    a socket on agency B. Each Redis pub/sub message on the user's channel is
    relayed as a text frame until the client disconnects.
    """
    redis: Redis = websocket.app.state.redis
    resolver: TenantResolver = websocket.app.state.tenant_resolver
    host = websocket.headers.get("host", "").split(":")[0].strip().lower()
    tenant: TenantContext | None = await resolver.resolve(host) if host else None
    if tenant is None or tenant.status == "suspended":
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    user_id = await redeem_ws_ticket(redis, ticket, tenant_id=tenant.id)
    if user_id is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    await _relay_user_channel(websocket, redis, user_id)


async def _relay_user_channel(websocket: WebSocket, redis: Redis, user_id: uuid.UUID) -> None:
    channel = user_channel(user_id)
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message["data"]
            await websocket.send_text(
                data.decode() if isinstance(data, bytes | bytearray) else str(data)
            )
    except Exception:
        # Client gone / socket closed — fall through to cleanup.
        pass
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()  # type: ignore[no-untyped-call]
