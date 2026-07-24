"""DB access for the notifications module. Every method's first arg is
``tenant_id`` (golden rule §5); RLS is the fail-closed safety net beneath it.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import record_notification_send
from app.modules.notifications.models import (
    Notification,
    NotificationChannel,
    NotificationDigestItem,
    NotificationPreference,
    NotificationSend,
    NotificationType,
    SendStatus,
)


class NotificationsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, obj: object) -> None:
        self.session.add(obj)

    async def flush(self) -> None:
        await self.session.flush()

    # ---- compliance boundary (§8.17): DSR erasure ----

    async def delete_all_for_user(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Hard-delete a user's notifications, preferences and pending digest
        items (erasure §10.12) — transient in-app messages, no business record
        to keep. ``notification_sends`` (a deliverability log) is left intact:
        it holds no PII beyond an already-deleted notification reference and is
        the audit record of what was attempted."""
        for model in (Notification, NotificationPreference, NotificationDigestItem):
            await self.session.execute(
                delete(model).where(model.tenant_id == tenant_id, model.user_id == user_id)
            )

    # ---- in-app notifications ----

    async def get(
        self, tenant_id: uuid.UUID, notification_id: uuid.UUID, user_id: uuid.UUID
    ) -> Notification | None:
        stmt = select(Notification).where(
            Notification.tenant_id == tenant_id,
            Notification.id == notification_id,
            Notification.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_id_for_send(
        self, tenant_id: uuid.UUID, notification_id: uuid.UUID
    ) -> Notification | None:
        """Fetch by id without a user scope — the delivery worker already holds
        the notification id and just needs the row's type to render it."""
        stmt = select(Notification).where(
            Notification.tenant_id == tenant_id, Notification.id == notification_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_user(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        unread_only: bool,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Notification]:
        """Keyset page on (created_at DESC, id DESC); returns limit+1 rows."""
        stmt = select(Notification).where(
            Notification.tenant_id == tenant_id, Notification.user_id == user_id
        )
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        if after is not None:
            stmt = stmt.where(
                or_(
                    Notification.created_at < after[0],
                    and_(Notification.created_at == after[0], Notification.id < after[1]),
                )
            )
        stmt = stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(
            limit + 1
        )
        return list((await self.session.execute(stmt)).scalars())

    async def unread_count(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.tenant_id == tenant_id,
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def mark_all_read(self, tenant_id: uuid.UUID, user_id: uuid.UUID, now: datetime) -> None:
        stmt = (
            update(Notification)
            .where(
                Notification.tenant_id == tenant_id,
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
            )
            .values(read_at=now)
        )
        await self.session.execute(stmt)

    # ---- preferences ----

    async def preferences_for_user(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[NotificationPreference]:
        stmt = select(NotificationPreference).where(
            NotificationPreference.tenant_id == tenant_id,
            NotificationPreference.user_id == user_id,
        )
        return list((await self.session.execute(stmt)).scalars())

    async def preferences_for_type(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID, type_: NotificationType
    ) -> dict[NotificationChannel, bool]:
        """Explicit per-channel decisions for one (user, type). Channels absent
        here fall back to the type default in the service."""
        stmt = select(NotificationPreference).where(
            NotificationPreference.tenant_id == tenant_id,
            NotificationPreference.user_id == user_id,
            NotificationPreference.type == type_,
        )
        rows = (await self.session.execute(stmt)).scalars()
        return {row.channel: row.enabled for row in rows}

    async def upsert_preference(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        type_: NotificationType,
        channel: NotificationChannel,
        enabled: bool,
    ) -> None:
        stmt = select(NotificationPreference).where(
            NotificationPreference.tenant_id == tenant_id,
            NotificationPreference.user_id == user_id,
            NotificationPreference.type == type_,
            NotificationPreference.channel == channel,
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            existing.enabled = enabled
        else:
            self.session.add(
                NotificationPreference(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    type=type_,
                    channel=channel,
                    enabled=enabled,
                )
            )

    # ---- delivery log ----

    def log_send(
        self,
        tenant_id: uuid.UUID,
        *,
        notification_id: uuid.UUID | None,
        channel: NotificationChannel,
        status: SendStatus,
        provider_message_id: str | None,
        error: str | None,
        sent_at: datetime | None,
    ) -> None:
        self.session.add(
            NotificationSend(
                tenant_id=tenant_id,
                notification_id=notification_id,
                channel=channel,
                status=status,
                provider_message_id=provider_message_id,
                error=error,
                sent_at=sent_at,
            )
        )
        # §14 delivery rate. Instrumented here rather than at the call sites
        # because this is the single write point for the delivery log — every
        # attempt, on every channel, passes through it. Counted per *attempt*
        # (not post-commit): the metric answers "of the deliveries we tried,
        # what fraction succeeded", so a retried send legitimately counts twice.
        record_notification_send(channel.value, status.value)

    # ---- digest queue ----

    def enqueue_digest_item(
        self,
        tenant_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        notification_id: uuid.UUID,
        channel: NotificationChannel,
    ) -> None:
        self.session.add(
            NotificationDigestItem(
                tenant_id=tenant_id,
                user_id=user_id,
                notification_id=notification_id,
                channel=channel,
            )
        )

    async def pending_digest_items(
        self, tenant_id: uuid.UUID
    ) -> list[tuple[NotificationDigestItem, Notification]]:
        """Unsent digest items joined to their notification, ordered by user so
        the sweep can batch per user in one pass."""
        stmt = (
            select(NotificationDigestItem, Notification)
            .join(Notification, Notification.id == NotificationDigestItem.notification_id)
            .where(
                NotificationDigestItem.tenant_id == tenant_id,
                NotificationDigestItem.sent_at.is_(None),
            )
            .order_by(NotificationDigestItem.user_id, NotificationDigestItem.created_at)
        )
        return [(item, note) for item, note in (await self.session.execute(stmt)).all()]

    async def mark_digest_items_sent(
        self, tenant_id: uuid.UUID, item_ids: Sequence[uuid.UUID], now: datetime
    ) -> None:
        if not item_ids:
            return
        stmt = (
            update(NotificationDigestItem)
            .where(
                NotificationDigestItem.tenant_id == tenant_id,
                NotificationDigestItem.id.in_(item_ids),
            )
            .values(sent_at=now)
        )
        await self.session.execute(stmt)
