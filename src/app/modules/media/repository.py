"""DB access for listing media. Every method takes ``tenant_id`` (golden rule
§5); listing-level ownership scoping happens in the service by resolving the
listing through the listings service first.
"""

import uuid

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.media.models import (
    PUBLIC_KINDS,
    ListingMedia,
    MediaKind,
    MediaStatus,
)


class MediaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _base(self, tenant_id: uuid.UUID) -> Select[tuple[ListingMedia]]:
        return select(ListingMedia).where(ListingMedia.tenant_id == tenant_id)

    async def get(
        self, tenant_id: uuid.UUID, media_id: uuid.UUID, *, for_update: bool = False
    ) -> ListingMedia | None:
        stmt = self._base(tenant_id).where(ListingMedia.id == media_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_listing(
        self, tenant_id: uuid.UUID, listing_id: uuid.UUID
    ) -> list[ListingMedia]:
        stmt = (
            self._base(tenant_id)
            .where(ListingMedia.listing_id == listing_id)
            .order_by(ListingMedia.position, ListingMedia.created_at, ListingMedia.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def list_public_for_listing(
        self, tenant_id: uuid.UUID, listing_id: uuid.UUID
    ) -> list[ListingMedia]:
        stmt = (
            self._base(tenant_id)
            .where(
                ListingMedia.listing_id == listing_id,
                ListingMedia.status == MediaStatus.READY,
                ListingMedia.kind.in_(PUBLIC_KINDS),
            )
            .order_by(
                ListingMedia.is_cover.desc(),
                ListingMedia.position,
                ListingMedia.created_at,
                ListingMedia.id,
            )
        )
        return list((await self.session.execute(stmt)).scalars())

    async def covers_for_listings(
        self, tenant_id: uuid.UUID, listing_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, ListingMedia]:
        """Best cover per listing: the marked cover, else the first ready photo.

        One ``DISTINCT ON`` query for a whole result page — never per row.
        """
        if not listing_ids:
            return {}
        stmt = (
            select(ListingMedia)
            .distinct(ListingMedia.listing_id)
            .where(
                ListingMedia.tenant_id == tenant_id,
                ListingMedia.listing_id.in_(listing_ids),
                ListingMedia.status == MediaStatus.READY,
                ListingMedia.kind == MediaKind.PHOTO,
            )
            .order_by(
                ListingMedia.listing_id,
                ListingMedia.is_cover.desc(),
                ListingMedia.position,
                ListingMedia.created_at,
                ListingMedia.id,
            )
        )
        rows = (await self.session.execute(stmt)).scalars()
        return {m.listing_id: m for m in rows}

    async def count_active_photos(self, tenant_id: uuid.UUID, listing_id: uuid.UUID) -> int:
        """Photos counted against the quota: everything not failed — a pending
        presigned slot reserves quota, or a flood of presign requests would
        bypass the cap entirely."""
        stmt = (
            select(func.count())
            .select_from(ListingMedia)
            .where(
                ListingMedia.tenant_id == tenant_id,
                ListingMedia.listing_id == listing_id,
                ListingMedia.kind == MediaKind.PHOTO,
                ListingMedia.status != MediaStatus.FAILED,
            )
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def next_position(self, tenant_id: uuid.UUID, listing_id: uuid.UUID) -> int:
        stmt = select(func.max(ListingMedia.position)).where(
            ListingMedia.tenant_id == tenant_id,
            ListingMedia.listing_id == listing_id,
        )
        current = (await self.session.execute(stmt)).scalar_one_or_none()
        return 0 if current is None else current + 1

    async def clear_cover(self, tenant_id: uuid.UUID, listing_id: uuid.UUID) -> None:
        await self.session.execute(
            update(ListingMedia)
            .where(
                ListingMedia.tenant_id == tenant_id,
                ListingMedia.listing_id == listing_id,
                ListingMedia.is_cover,
            )
            .values(is_cover=False)
        )

    def add(self, media: ListingMedia) -> None:
        self.session.add(media)

    async def delete(self, media: ListingMedia) -> None:
        await self.session.delete(media)

    async def flush(self) -> None:
        await self.session.flush()
