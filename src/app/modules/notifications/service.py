"""Notifications business logic (§8.12).

``notify()`` is the one internal API every other module routes user-facing
notifications through. It is **not** a router endpoint — other modules' services
call it directly (leads, appointments), constructing the service via
``build_notifications_boundary(session, redis)`` so there is no cross-module
model/repository import.

``notify()``:

1. Resolves the recipient's per-type channel preferences (explicit rows override
   the type default; a user with no rows still gets the default channel set).
2. Writes the in-app row (always — the in-app channel is the durable record;
   ``GET /me/notifications`` reads it back regardless of live delivery).
3. Publishes to the user's Redis channel post-commit so a connected WebSocket
   gets an instant ping (best-effort — a rolled-back transaction never pings,
   and a client reconciles against the DB on connect either way).
4. For each enabled external channel (email/sms/whatsapp): either enqueues the
   ``deliver_notification`` Celery task immediately, or — when the type is
   digest-eligible and the user is inside their quiet-hours window — parks it in
   the digest queue for the batching sweep instead of sending at 3am.

Steps 3 and 4's *side effects* are registered via ``on_commit`` so a rolled-back
caller transaction fires nothing. Worker callers get the same behaviour because
``workers.db`` drains post-commit callbacks after its scoped transaction, exactly
like the request session does.
"""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, tzinfo
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import structlog
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionDep, on_commit
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.tenancy import TenantContext
from app.modules.notifications.models import (
    EXTERNAL_CHANNELS,
    Notification,
    NotificationChannel,
    NotificationType,
)
from app.modules.notifications.repository import NotificationsRepository
from app.modules.notifications.schemas import PreferencesUpdateIn
from app.modules.notifications.types import definition_for

logger = structlog.get_logger(__name__)


def user_channel(user_id: uuid.UUID) -> str:
    """Redis pub/sub channel for one user's live notifications — one channel
    per user, not per tenant, so fan-out stays O(1) per recipient (§3)."""
    return f"notify:user:{user_id}"


@dataclass(frozen=True, slots=True)
class _QuietHours:
    start: time
    end: time
    tz: tzinfo

    def contains(self, moment: datetime) -> bool:
        local = moment.astimezone(self.tz).time()
        if self.start <= self.end:
            return self.start <= local < self.end
        # Window wraps midnight (e.g. 22:00-07:00).
        return local >= self.start or local < self.end


def _parse_hhmm(value: Any) -> time | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    hh, _, mm = value.partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return time(hour=h, minute=m)
    return None


def _tenant_quiet_hours(tenant: TenantContext) -> _QuietHours | None:
    """Defensive-JSONB settings (same stance as appointments/mortgage): a
    malformed ``settings.notifications.quiet_hours`` degrades to *no* quiet
    hours rather than dropping or misrouting a notification."""
    raw = tenant.settings.get("notifications")
    if not isinstance(raw, dict):
        return None
    quiet = raw.get("quiet_hours")
    if not isinstance(quiet, dict):
        return None
    start = _parse_hhmm(quiet.get("start"))
    end = _parse_hhmm(quiet.get("end"))
    if start is None or end is None or start == end:
        return None
    tz: tzinfo = UTC
    zone = quiet.get("timezone")
    if isinstance(zone, str) and zone and zone.upper() != "UTC":
        try:
            tz = ZoneInfo(zone)
        except Exception:
            logger.warning("notifications_bad_quiet_hours_tz", zone=zone, tenant_id=str(tenant.id))
    return _QuietHours(start=start, end=end, tz=tz)


class NotificationsService:
    def __init__(self, repo: NotificationsRepository, redis: Redis | None) -> None:
        self.repo = repo
        self.redis = redis

    # ---- the internal fan-out API ----

    async def notify(
        self,
        tenant: TenantContext,
        *,
        user_id: uuid.UUID,
        type: NotificationType,
        payload: Mapping[str, Any],
        locale: str,
    ) -> Notification:
        """Fan out one notification to a single user across their enabled
        channels. Returns the persisted in-app row."""
        definition = definition_for(type)
        data = dict(payload)

        notification = Notification(
            tenant_id=tenant.id, user_id=user_id, type=type, payload=data
        )
        self.repo.add(notification)
        await self.repo.flush()
        notification_id = notification.id

        channels = await self._resolved_channels(tenant.id, user_id, type)

        # Live WS push (in-app): best-effort, post-commit.
        if NotificationChannel.IN_APP in channels:
            ws_event = json.dumps(
                {
                    "id": str(notification_id),
                    "type": type.value,
                    "payload": data,
                    "createdAt": notification.created_at.isoformat()
                    if notification.created_at
                    else datetime.now(UTC).isoformat(),
                }
            )
            self._defer_ws_publish(user_id, ws_event)

        # External channels: send now, or park for the digest during quiet hours.
        quiet = _tenant_quiet_hours(tenant)
        in_quiet = quiet is not None and quiet.contains(datetime.now(UTC))
        for channel in EXTERNAL_CHANNELS:
            if channel not in channels:
                continue
            if definition.digest_eligible and in_quiet:
                self.repo.enqueue_digest_item(
                    tenant.id,
                    user_id=user_id,
                    notification_id=notification_id,
                    channel=channel,
                )
            else:
                self._defer_send(tenant.id, notification_id, channel, locale, data)

        return notification

    async def _resolved_channels(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID, type: NotificationType
    ) -> set[NotificationChannel]:
        """Explicit preference rows override the type default; channels with no
        row fall back to whether the default set includes them."""
        default = definition_for(type).default_channels
        explicit = await self.repo.preferences_for_type(tenant_id, user_id, type)
        resolved: set[NotificationChannel] = set()
        for channel in NotificationChannel:
            enabled = explicit.get(channel, channel in default)
            if enabled:
                resolved.add(channel)
        return resolved

    def _defer_ws_publish(self, user_id: uuid.UUID, event: str) -> None:
        redis = self.redis
        if redis is None:
            return

        async def _publish() -> None:
            try:
                await redis.publish(user_channel(user_id), event)
            except Exception:
                # A live ping is best-effort; the durable in-app row is the
                # source of truth (client reconciles on fetch/connect).
                logger.warning("notification_ws_publish_failed", user_id=str(user_id))

        on_commit(self.repo.session, _publish)

    def _defer_send(
        self,
        tenant_id: uuid.UUID,
        notification_id: uuid.UUID,
        channel: NotificationChannel,
        locale: str,
        payload: Mapping[str, Any],
    ) -> None:
        # Lazy import to dodge the module-load cycle (task → notifications
        # service → ... ). Enqueued post-commit so a rolled-back notify sends
        # nothing.
        from app.workers.tasks.notifications import deliver_notification

        data = dict(payload)

        async def _enqueue() -> None:
            deliver_notification.delay(
                tenant_id=str(tenant_id),
                notification_id=str(notification_id),
                channel=channel.value,
                locale=locale,
                payload=data,
            )

        on_commit(self.repo.session, _enqueue)

    # ---- /me surface ----

    async def list_for_user(
        self,
        tenant: TenantContext,
        user_id: uuid.UUID,
        *,
        unread_only: bool,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[Notification], str | None]:
        page_size = clamp_limit(limit)
        after = _decode_keyset(cursor) if cursor else None
        rows = await self.repo.list_for_user(
            tenant.id, user_id, unread_only=unread_only, after=after, limit=page_size
        )
        items = rows[:page_size]
        next_cursor = _next_cursor(rows, items, page_size)
        return items, next_cursor

    async def unread_count(self, tenant: TenantContext, user_id: uuid.UUID) -> int:
        return await self.repo.unread_count(tenant.id, user_id)

    async def mark_read(
        self,
        tenant: TenantContext,
        user_id: uuid.UUID,
        *,
        ids: list[uuid.UUID] | None,
        all_: bool,
    ) -> None:
        now = datetime.now(UTC)
        if all_:
            await self.repo.mark_all_read(tenant.id, user_id, now)
            return
        for notification_id in ids or []:
            row = await self.repo.get(tenant.id, notification_id, user_id)
            # Unknown/foreign ids are silently ignored — marking read is
            # idempotent and leaks no existence oracle.
            if row is not None and row.read_at is None:
                row.read_at = now
        await self.repo.flush()

    # ---- compliance boundary (§8.17): DSR export + erasure ----

    async def export_for_user(
        self, tenant: TenantContext, user_id: uuid.UUID
    ) -> dict[str, object]:
        """Read-only dump of a user's in-app notifications (§10.12)."""
        rows = await self.repo.list_for_user(
            tenant.id, user_id, unread_only=False, after=None, limit=1000
        )
        return {
            "notifications": [
                {
                    "id": str(n.id),
                    "type": n.type.value,
                    "payload": n.payload,
                    "read_at": n.read_at.isoformat() if n.read_at else None,
                    "created_at": n.created_at.isoformat(),
                }
                for n in rows
            ]
        }

    async def erase_for_user(self, tenant: TenantContext, user_id: uuid.UUID) -> None:
        """Erasure (§10.12): delete a user's notifications, preferences and
        pending digest items."""
        await self.repo.delete_all_for_user(tenant.id, user_id)

    # ---- preferences ----

    async def get_preferences(
        self, tenant: TenantContext, user_id: uuid.UUID
    ) -> dict[NotificationType, dict[NotificationChannel, bool]]:
        """The effective matrix per type: the type default overlaid with the
        user's explicit rows. Every type is represented so the UI can render a
        full grid even for an untouched user."""
        rows = await self.repo.preferences_for_user(tenant.id, user_id)
        explicit: dict[tuple[NotificationType, NotificationChannel], bool] = {
            (r.type, r.channel): r.enabled for r in rows
        }
        matrix: dict[NotificationType, dict[NotificationChannel, bool]] = {}
        for type_ in NotificationType:
            default = definition_for(type_).default_channels
            matrix[type_] = {
                channel: explicit.get((type_, channel), channel in default)
                for channel in NotificationChannel
            }
        return matrix

    async def update_preferences(
        self, tenant: TenantContext, user_id: uuid.UUID, data: PreferencesUpdateIn
    ) -> dict[NotificationType, dict[NotificationChannel, bool]]:
        for type_pref in data.types:
            for channel_pref in type_pref.channels:
                await self.repo.upsert_preference(
                    tenant.id,
                    user_id,
                    type_pref.type,
                    channel_pref.channel,
                    channel_pref.enabled,
                )
        await self.repo.flush()
        return await self.get_preferences(tenant, user_id)


def _next_cursor(
    rows: list[Notification], items: list[Notification], page_size: int
) -> str | None:
    if len(rows) <= page_size:
        return None
    last = items[-1]
    return encode_cursor({"created_at": last.created_at.isoformat(), "id": str(last.id)})


def _decode_keyset(cursor: str) -> tuple[datetime, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(values["created_at"]), uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def get_notifications_service(session: SessionDep, request: Request) -> NotificationsService:
    return NotificationsService(NotificationsRepository(session), request.app.state.redis)


def build_notifications_boundary(
    session: AsyncSession, redis: Redis | None = None
) -> NotificationsService:
    """For dependent composition — other modules' services call ``notify()``
    without needing ``request.app.state``. ``redis`` is optional: when absent
    (a worker with no client wired), the live WS push is simply skipped and the
    durable in-app row + external sends still happen."""
    return NotificationsService(NotificationsRepository(session), redis)


NotificationsServiceDep = Annotated[
    NotificationsService, Depends(get_notifications_service)
]
