"""DB access for outbound webhooks (§8.14, §10.9). Every method's first arg is
``tenant_id`` (golden rule §5)."""

import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.webhooks.models import WebhookDelivery, WebhookEndpoint


class WebhookRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, obj: WebhookEndpoint | WebhookDelivery) -> None:
        self.session.add(obj)

    async def flush(self) -> None:
        await self.session.flush()

    # ---- endpoints ----

    async def get_endpoint(
        self, tenant_id: uuid.UUID, endpoint_id: uuid.UUID, *, for_update: bool = False
    ) -> WebhookEndpoint | None:
        stmt = select(WebhookEndpoint).where(
            WebhookEndpoint.tenant_id == tenant_id,
            WebhookEndpoint.id == endpoint_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_endpoints(self, tenant_id: uuid.UUID) -> list[WebhookEndpoint]:
        stmt = (
            select(WebhookEndpoint)
            .where(WebhookEndpoint.tenant_id == tenant_id)
            .order_by(WebhookEndpoint.created_at.desc(), WebhookEndpoint.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def endpoints_for_event(
        self, tenant_id: uuid.UUID, event_type: str
    ) -> list[WebhookEndpoint]:
        """Active, non-tripped endpoints subscribed to ``event_type`` — the
        fan-out set for one domain event. Uses the JSONB ``@>`` containment the
        codebase uses for listings.features / blog.tags."""
        stmt = select(WebhookEndpoint).where(
            WebhookEndpoint.tenant_id == tenant_id,
            WebhookEndpoint.is_active.is_(True),
            WebhookEndpoint.circuit_open.is_(False),
            WebhookEndpoint.events.contains([event_type]),
        )
        return list((await self.session.execute(stmt)).scalars())

    async def delete_endpoint(self, endpoint: WebhookEndpoint) -> None:
        await self.session.delete(endpoint)

    # ---- deliveries ----

    async def get_delivery(
        self, tenant_id: uuid.UUID, delivery_id: uuid.UUID, *, for_update: bool = False
    ) -> WebhookDelivery | None:
        stmt = select(WebhookDelivery).where(
            WebhookDelivery.tenant_id == tenant_id,
            WebhookDelivery.id == delivery_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_deliveries(
        self,
        tenant_id: uuid.UUID,
        *,
        endpoint_id: uuid.UUID | None,
        after: tuple[str, uuid.UUID] | None,
        limit: int,
    ) -> list[WebhookDelivery]:
        """Keyset page on (created_at DESC, id DESC); returns limit+1 rows."""
        stmt = select(WebhookDelivery).where(WebhookDelivery.tenant_id == tenant_id)
        if endpoint_id is not None:
            stmt = stmt.where(WebhookDelivery.endpoint_id == endpoint_id)
        if after is not None:
            after_ts = datetime.fromisoformat(after[0])
            stmt = stmt.where(
                or_(
                    WebhookDelivery.created_at < after_ts,
                    and_(
                        WebhookDelivery.created_at == after_ts,
                        WebhookDelivery.id < after[1],
                    ),
                )
            )
        stmt = stmt.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc()).limit(
            limit + 1
        )
        return list((await self.session.execute(stmt)).scalars())
