"""DB access for portal sync state (§8.14). Every method's first arg is
``tenant_id`` (golden rule §5)."""

import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.syndication.models import PortalSyncState


class SyndicationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, state: PortalSyncState) -> None:
        self.session.add(state)

    async def flush(self) -> None:
        await self.session.flush()

    async def get(
        self,
        tenant_id: uuid.UUID,
        listing_id: uuid.UUID,
        portal_key: str,
        *,
        for_update: bool = False,
    ) -> PortalSyncState | None:
        stmt = select(PortalSyncState).where(
            PortalSyncState.tenant_id == tenant_id,
            PortalSyncState.listing_id == listing_id,
            PortalSyncState.portal_key == portal_key,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def for_listing(
        self, tenant_id: uuid.UUID, listing_id: uuid.UUID
    ) -> list[PortalSyncState]:
        stmt = (
            select(PortalSyncState)
            .where(
                PortalSyncState.tenant_id == tenant_id,
                PortalSyncState.listing_id == listing_id,
            )
            .order_by(PortalSyncState.portal_key)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def list_page(
        self,
        tenant_id: uuid.UUID,
        *,
        portal_key: str | None,
        after: tuple[str, uuid.UUID] | None,
        limit: int,
    ) -> list[PortalSyncState]:
        """Keyset page on (updated_at DESC, id DESC) — cursor carries the
        ISO ``updated_at``; returns limit+1 rows."""
        stmt = select(PortalSyncState).where(PortalSyncState.tenant_id == tenant_id)
        if portal_key is not None:
            stmt = stmt.where(PortalSyncState.portal_key == portal_key)
        if after is not None:
            after_ts = datetime.fromisoformat(after[0])
            stmt = stmt.where(
                or_(
                    PortalSyncState.updated_at < after_ts,
                    and_(
                        PortalSyncState.updated_at == after_ts,
                        PortalSyncState.id < after[1],
                    ),
                )
            )
        stmt = stmt.order_by(PortalSyncState.updated_at.desc(), PortalSyncState.id.desc()).limit(
            limit + 1
        )
        return list((await self.session.execute(stmt)).scalars())
