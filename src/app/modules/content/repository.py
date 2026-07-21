"""DB access for content pages and legal pages. Every method takes
``tenant_id`` (golden rule §5); RLS is the safety net, not the only guard.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.content.models import ContentPage, LegalKind, LegalPage, PageStatus


class ContentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, obj: ContentPage | LegalPage) -> None:
        self.session.add(obj)

    async def flush(self) -> None:
        await self.session.flush()

    # ---- pages ----

    async def get_page(self, tenant_id: uuid.UUID, page_id: uuid.UUID) -> ContentPage | None:
        stmt = select(ContentPage).where(
            ContentPage.tenant_id == tenant_id, ContentPage.id == page_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_page_by_slug(self, tenant_id: uuid.UUID, slug: str) -> ContentPage | None:
        stmt = select(ContentPage).where(
            ContentPage.tenant_id == tenant_id, ContentPage.slug == slug
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_published_page_by_slug(
        self, tenant_id: uuid.UUID, slug: str
    ) -> ContentPage | None:
        stmt = select(ContentPage).where(
            ContentPage.tenant_id == tenant_id,
            ContentPage.slug == slug,
            ContentPage.status == PageStatus.PUBLISHED,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_pages(
        self,
        tenant_id: uuid.UUID,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[ContentPage]:
        """Keyset page on (created_at DESC, id DESC); returns limit+1 rows."""
        stmt = select(ContentPage).where(ContentPage.tenant_id == tenant_id)
        if after is not None:
            stmt = stmt.where(
                (ContentPage.created_at < after[0])
                | ((ContentPage.created_at == after[0]) & (ContentPage.id < after[1]))
            )
        stmt = stmt.order_by(ContentPage.created_at.desc(), ContentPage.id.desc()).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    async def count_pages(self, tenant_id: uuid.UUID) -> int:
        stmt = (
            select(func.count()).select_from(ContentPage).where(ContentPage.tenant_id == tenant_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def published_pages_for_sitemap(self, tenant_id: uuid.UUID) -> list[ContentPage]:
        stmt = (
            select(ContentPage)
            .where(
                ContentPage.tenant_id == tenant_id,
                ContentPage.status == PageStatus.PUBLISHED,
            )
            .order_by(ContentPage.created_at.desc(), ContentPage.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def delete_page(self, page: ContentPage) -> None:
        await self.session.delete(page)

    # ---- legal pages ----

    async def get_current_legal(
        self, tenant_id: uuid.UUID, kind: LegalKind, *, for_update: bool = False
    ) -> LegalPage | None:
        stmt = select(LegalPage).where(
            LegalPage.tenant_id == tenant_id,
            LegalPage.kind == kind,
            LegalPage.is_current.is_(True),
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_current_legal(self, tenant_id: uuid.UUID) -> list[LegalPage]:
        stmt = (
            select(LegalPage)
            .where(LegalPage.tenant_id == tenant_id, LegalPage.is_current.is_(True))
            .order_by(LegalPage.kind)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def list_legal_history(self, tenant_id: uuid.UUID, kind: LegalKind) -> list[LegalPage]:
        stmt = (
            select(LegalPage)
            .where(LegalPage.tenant_id == tenant_id, LegalPage.kind == kind)
            .order_by(LegalPage.version.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def max_legal_version(self, tenant_id: uuid.UUID, kind: LegalKind) -> int:
        stmt = select(func.max(LegalPage.version)).where(
            LegalPage.tenant_id == tenant_id, LegalPage.kind == kind
        )
        return (await self.session.execute(stmt)).scalar_one() or 0
