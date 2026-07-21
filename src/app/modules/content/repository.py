"""DB access for content pages and legal pages. Every method takes
``tenant_id`` (golden rule §5); RLS is the safety net, not the only guard.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.content.models import (
    ContentPage,
    LegalKind,
    LegalPage,
    MarketReport,
    NeighborhoodGuide,
    PageStatus,
    ReportStatus,
)

ContentRow = ContentPage | LegalPage | NeighborhoodGuide | MarketReport


class ContentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, obj: ContentRow) -> None:
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

    async def published_pages_for_sitemap(
        self, tenant_id: uuid.UUID, *, limit: int
    ) -> list[ContentPage]:
        stmt = (
            select(ContentPage)
            .where(
                ContentPage.tenant_id == tenant_id,
                ContentPage.status == PageStatus.PUBLISHED,
            )
            .order_by(ContentPage.created_at.desc(), ContentPage.id.desc())
            .limit(limit)
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

    # ---- neighborhood guides ----

    async def get_guide(
        self, tenant_id: uuid.UUID, guide_id: uuid.UUID
    ) -> NeighborhoodGuide | None:
        stmt = select(NeighborhoodGuide).where(
            NeighborhoodGuide.tenant_id == tenant_id, NeighborhoodGuide.id == guide_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_published_guide_by_slug(
        self, tenant_id: uuid.UUID, slug: str
    ) -> NeighborhoodGuide | None:
        stmt = select(NeighborhoodGuide).where(
            NeighborhoodGuide.tenant_id == tenant_id,
            NeighborhoodGuide.slug == slug,
            NeighborhoodGuide.status == PageStatus.PUBLISHED,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_guides(
        self,
        tenant_id: uuid.UUID,
        *,
        published_only: bool,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[NeighborhoodGuide]:
        """Keyset page on (created_at DESC, id DESC); returns limit+1 rows."""
        stmt = select(NeighborhoodGuide).where(NeighborhoodGuide.tenant_id == tenant_id)
        if published_only:
            stmt = stmt.where(NeighborhoodGuide.status == PageStatus.PUBLISHED)
        if after is not None:
            stmt = stmt.where(
                (NeighborhoodGuide.created_at < after[0])
                | (
                    (NeighborhoodGuide.created_at == after[0])
                    & (NeighborhoodGuide.id < after[1])
                )
            )
        stmt = stmt.order_by(
            NeighborhoodGuide.created_at.desc(), NeighborhoodGuide.id.desc()
        ).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    async def count_guides(self, tenant_id: uuid.UUID, *, published_only: bool) -> int:
        stmt = (
            select(func.count())
            .select_from(NeighborhoodGuide)
            .where(NeighborhoodGuide.tenant_id == tenant_id)
        )
        if published_only:
            stmt = stmt.where(NeighborhoodGuide.status == PageStatus.PUBLISHED)
        return (await self.session.execute(stmt)).scalar_one()

    async def published_guides_with_boundary(
        self, tenant_id: uuid.UUID
    ) -> list[NeighborhoodGuide]:
        """Every published guide that has a boundary — the Beat stats job's
        input (guides without a boundary get no auto stats)."""
        stmt = select(NeighborhoodGuide).where(
            NeighborhoodGuide.tenant_id == tenant_id,
            NeighborhoodGuide.status == PageStatus.PUBLISHED,
            NeighborhoodGuide.boundary.is_not(None),
        )
        return list((await self.session.execute(stmt)).scalars())

    async def published_guides_for_sitemap(
        self, tenant_id: uuid.UUID, *, limit: int
    ) -> list[NeighborhoodGuide]:
        stmt = (
            select(NeighborhoodGuide)
            .where(
                NeighborhoodGuide.tenant_id == tenant_id,
                NeighborhoodGuide.status == PageStatus.PUBLISHED,
            )
            .order_by(NeighborhoodGuide.created_at.desc(), NeighborhoodGuide.id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def delete_guide(self, guide: NeighborhoodGuide) -> None:
        await self.session.delete(guide)

    # ---- market reports ----

    async def get_report(
        self, tenant_id: uuid.UUID, report_id: uuid.UUID, *, for_update: bool = False
    ) -> MarketReport | None:
        stmt = select(MarketReport).where(
            MarketReport.tenant_id == tenant_id, MarketReport.id == report_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_published_report_by_slug(
        self, tenant_id: uuid.UUID, slug: str
    ) -> MarketReport | None:
        stmt = select(MarketReport).where(
            MarketReport.tenant_id == tenant_id,
            MarketReport.slug == slug,
            MarketReport.status.in_((ReportStatus.PUBLISHED, ReportStatus.READY)),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_reports(
        self,
        tenant_id: uuid.UUID,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[MarketReport]:
        stmt = select(MarketReport).where(MarketReport.tenant_id == tenant_id)
        if after is not None:
            stmt = stmt.where(
                (MarketReport.created_at < after[0])
                | ((MarketReport.created_at == after[0]) & (MarketReport.id < after[1]))
            )
        stmt = stmt.order_by(MarketReport.created_at.desc(), MarketReport.id.desc()).limit(
            limit + 1
        )
        return list((await self.session.execute(stmt)).scalars())

    async def count_reports(self, tenant_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(MarketReport)
            .where(MarketReport.tenant_id == tenant_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def delete_report(self, report: MarketReport) -> None:
        await self.session.delete(report)
