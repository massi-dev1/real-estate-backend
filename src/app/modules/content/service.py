"""Content CMS business logic (§8.10, slice 1).

Pages carry a draft/published lifecycle with stateless HMAC **preview tokens**
(``sign_value``, purpose-separated, value pinned to tenant + page id) so a
draft is shareable without auth and forged/foreign-tenant tokens 404 — the
same token pattern used for valuations and iCal feeds, no Redis TTL to outlive.

Legal pages are versioned: publishing a new version flips the prior current
row (``SELECT … FOR UPDATE`` so concurrent publishes serialize) and inserts a
new row — history is append-only, so consent text is always provable (§10.12).
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from fastapi import Depends, Request
from sqlalchemy.exc import IntegrityError

from app.common.geo import multipolygon_rings, to_multipolygon
from app.core.config import Settings
from app.core.database import SessionDep, on_commit
from app.core.exceptions import ConflictError, NotFoundError
from app.core.i18n import DEFAULT_LOCALE, pick_localized
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.security import sign_value, unsign_value
from app.core.storage import ObjectStorage
from app.core.tenancy import TenantContext
from app.modules.content.models import (
    ContentPage,
    LegalKind,
    LegalPage,
    MarketReport,
    NeighborhoodGuide,
    PageStatus,
    ReportStatus,
)
from app.modules.content.repository import ContentRepository
from app.modules.content.schemas import (
    GuideCreate,
    GuideUpdate,
    LegalPageCreate,
    PageCreate,
    PageUpdate,
    ReportCreate,
    ReportDownloadCreate,
    ReportUpdate,
)
from app.modules.leads.service import LeadsService, get_leads_service
from app.modules.listings.service import ListingService, get_listing_service

logger = structlog.get_logger(__name__)

_PREVIEW_PURPOSE = "page-preview"

# Content pages share the sitemap's 50k-URL budget with listings. A generous
# per-tenant page cap keeps the combined sitemap within the sitemaps.org limit
# without a full sitemap index (which arrives with a later content part).
SITEMAP_MAX_PAGES = 10_000


class ContentService:
    def __init__(
        self,
        repo: ContentRepository,
        settings: Settings,
        listings: ListingService,
        leads: LeadsService,
        storage: ObjectStorage,
    ) -> None:
        self.repo = repo
        self._settings = settings  # HMAC preview-token signing only
        self.listings = listings
        self.leads = leads
        self.storage = storage

    # ---- pages: portal ----

    async def create_page(self, tenant: TenantContext, data: PageCreate) -> ContentPage:
        page = ContentPage(
            tenant_id=tenant.id,
            slug=data.slug,
            title=data.title,
            blocks=[b.model_dump() for b in data.blocks],
            seo_meta=data.seo_meta.model_dump(exclude_none=True) if data.seo_meta else {},
            status=data.status,
            published_at=datetime.now(UTC) if data.status == PageStatus.PUBLISHED else None,
        )
        self.repo.add(page)
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError("A page with this slug already exists.") from exc
        return page

    async def get_page(self, tenant: TenantContext, page_id: uuid.UUID) -> ContentPage:
        page = await self.repo.get_page(tenant.id, page_id)
        if page is None:
            raise NotFoundError("Page not found.")
        return page

    async def list_pages(
        self, tenant: TenantContext, *, cursor: str | None, limit: int | None
    ) -> tuple[list[ContentPage], str | None, int]:
        page_size = clamp_limit(limit)
        after = _decode_keyset(cursor) if cursor else None
        rows = await self.repo.list_pages(tenant.id, after=after, limit=page_size)
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = encode_cursor(
                {"created_at": last.created_at.isoformat(), "id": str(last.id)}
            )
        total = await self.repo.count_pages(tenant.id)
        return items, next_cursor, total

    async def update_page(
        self, tenant: TenantContext, page_id: uuid.UUID, data: PageUpdate
    ) -> ContentPage:
        page = await self.get_page(tenant, page_id)
        fields = data.model_dump(exclude_unset=True)
        if "blocks" in fields:
            page.blocks = [b.model_dump() for b in (data.blocks or [])]
            fields.pop("blocks")
        if "seo_meta" in fields:
            page.seo_meta = data.seo_meta.model_dump(exclude_none=True) if data.seo_meta else {}
            fields.pop("seo_meta")
        status_before = page.status
        for key, value in fields.items():
            setattr(page, key, value)
        # First transition into published stamps published_at; it is not reset
        # on later edits (the page stays "published since" its first go-live).
        if (
            page.status == PageStatus.PUBLISHED
            and status_before != PageStatus.PUBLISHED
            and page.published_at is None
        ):
            page.published_at = datetime.now(UTC)
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError("A page with this slug already exists.") from exc
        return page

    async def publish_page(self, tenant: TenantContext, page_id: uuid.UUID) -> ContentPage:
        page = await self.get_page(tenant, page_id)
        if page.status != PageStatus.PUBLISHED:
            page.status = PageStatus.PUBLISHED
            if page.published_at is None:
                page.published_at = datetime.now(UTC)
            await self.repo.flush()
        return page

    async def unpublish_page(self, tenant: TenantContext, page_id: uuid.UUID) -> ContentPage:
        page = await self.get_page(tenant, page_id)
        if page.status != PageStatus.DRAFT:
            page.status = PageStatus.DRAFT
            await self.repo.flush()
        return page

    async def delete_page(self, tenant: TenantContext, page_id: uuid.UUID) -> None:
        page = await self.get_page(tenant, page_id)
        await self.repo.delete_page(page)
        await self.repo.flush()

    def preview_token(self, tenant: TenantContext, page: ContentPage) -> str:
        return sign_value(_PREVIEW_PURPOSE, f"{tenant.id}:{page.id}", self._settings)

    # ---- pages: public ----

    async def get_public_page(self, tenant: TenantContext, slug: str) -> ContentPage:
        page = await self.repo.get_published_page_by_slug(tenant.id, slug)
        if page is None:
            raise NotFoundError("Page not found.")
        return page

    async def get_preview_page(self, tenant: TenantContext, slug: str, token: str) -> ContentPage:
        """A draft (or published) page addressed by slug, gated by a preview
        token that pins the page's id to this tenant. Forged/foreign tokens and
        a slug mismatch are all indistinguishable 404s (no oracle)."""
        value = unsign_value(_PREVIEW_PURPOSE, token, self._settings)
        if value is None:
            raise NotFoundError("Page not found.")
        tenant_part, sep, id_part = value.partition(":")
        if not sep or tenant_part != str(tenant.id):
            raise NotFoundError("Page not found.")
        try:
            page_id = uuid.UUID(id_part)
        except ValueError:
            raise NotFoundError("Page not found.") from None
        page = await self.repo.get_page(tenant.id, page_id)
        if page is None or page.slug != slug:
            raise NotFoundError("Page not found.")
        return page

    async def sitemap_pages(self, tenant: TenantContext) -> list[ContentPage]:
        # Bounded like the listings sitemap so the combined output stays within
        # the 50k-URL cap sitemaps.org mandates (§8.3).
        return await self.repo.published_pages_for_sitemap(tenant.id, limit=SITEMAP_MAX_PAGES)

    # ---- legal pages ----

    async def publish_legal_version(
        self, tenant: TenantContext, data: LegalPageCreate
    ) -> LegalPage:
        """Insert a new version and flip the prior current one — atomic, and
        serialized per (tenant, kind) by the FOR UPDATE lock so two concurrent
        publishes can't both claim ``is_current``."""
        current = await self.repo.get_current_legal(tenant.id, data.kind, for_update=True)
        if current is not None:
            current.is_current = False
        next_version = await self.repo.max_legal_version(tenant.id, data.kind) + 1
        page = LegalPage(
            tenant_id=tenant.id,
            kind=data.kind,
            version=next_version,
            body=data.body,
            effective_at=data.effective_at or datetime.now(UTC),
            is_current=True,
        )
        self.repo.add(page)
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            # Lost the race to another publisher between the flip and insert.
            raise ConflictError("A newer version was just published; retry.") from exc
        return page

    async def get_current_legal(self, tenant: TenantContext, kind: LegalKind) -> LegalPage:
        page = await self.repo.get_current_legal(tenant.id, kind)
        if page is None:
            raise NotFoundError("Legal page not found.")
        return page

    async def list_current_legal(self, tenant: TenantContext) -> list[LegalPage]:
        return await self.repo.list_current_legal(tenant.id)

    async def list_legal_history(self, tenant: TenantContext, kind: LegalKind) -> list[LegalPage]:
        return await self.repo.list_legal_history(tenant.id, kind)

    # ---- neighborhood guides: portal ----

    async def create_guide(self, tenant: TenantContext, data: GuideCreate) -> NeighborhoodGuide:
        guide = NeighborhoodGuide(
            tenant_id=tenant.id,
            slug=data.slug,
            name=data.name,
            body=data.body or {},
            boundary=to_multipolygon(data.boundary) if data.boundary else None,
            seo_meta=data.seo_meta.model_dump(exclude_none=True) if data.seo_meta else {},
            status=data.status,
            published_at=datetime.now(UTC) if data.status == PageStatus.PUBLISHED else None,
        )
        self.repo.add(guide)
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError("A guide with this slug already exists.") from exc
        return guide

    async def get_guide(self, tenant: TenantContext, guide_id: uuid.UUID) -> NeighborhoodGuide:
        guide = await self.repo.get_guide(tenant.id, guide_id)
        if guide is None:
            raise NotFoundError("Guide not found.")
        return guide

    async def list_guides(
        self, tenant: TenantContext, *, cursor: str | None, limit: int | None
    ) -> tuple[list[NeighborhoodGuide], str | None, int]:
        page_size = clamp_limit(limit)
        after = _decode_keyset(cursor) if cursor else None
        rows = await self.repo.list_guides(
            tenant.id, published_only=False, after=after, limit=page_size
        )
        items = rows[:page_size]
        next_cursor = _guide_cursor(items[-1]) if len(rows) > page_size else None
        total = await self.repo.count_guides(tenant.id, published_only=False)
        return items, next_cursor, total

    async def update_guide(
        self, tenant: TenantContext, guide_id: uuid.UUID, data: GuideUpdate
    ) -> NeighborhoodGuide:
        guide = await self.get_guide(tenant, guide_id)
        fields = data.model_dump(exclude_unset=True)
        if "boundary" in fields:
            guide.boundary = to_multipolygon(data.boundary) if data.boundary else None
            fields.pop("boundary")
        if "seo_meta" in fields:
            guide.seo_meta = data.seo_meta.model_dump(exclude_none=True) if data.seo_meta else {}
            fields.pop("seo_meta")
        status_before = guide.status
        for key, value in fields.items():
            setattr(guide, key, value)
        if (
            guide.status == PageStatus.PUBLISHED
            and status_before != PageStatus.PUBLISHED
            and guide.published_at is None
        ):
            guide.published_at = datetime.now(UTC)
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError("A guide with this slug already exists.") from exc
        return guide

    async def publish_guide(self, tenant: TenantContext, guide_id: uuid.UUID) -> NeighborhoodGuide:
        guide = await self.get_guide(tenant, guide_id)
        if guide.status != PageStatus.PUBLISHED:
            guide.status = PageStatus.PUBLISHED
            if guide.published_at is None:
                guide.published_at = datetime.now(UTC)
            await self.repo.flush()
        return guide

    async def unpublish_guide(
        self, tenant: TenantContext, guide_id: uuid.UUID
    ) -> NeighborhoodGuide:
        guide = await self.get_guide(tenant, guide_id)
        if guide.status != PageStatus.DRAFT:
            guide.status = PageStatus.DRAFT
            await self.repo.flush()
        return guide

    async def delete_guide(self, tenant: TenantContext, guide_id: uuid.UUID) -> None:
        guide = await self.get_guide(tenant, guide_id)
        await self.repo.delete_guide(guide)
        await self.repo.flush()

    # ---- neighborhood guides: public ----

    async def list_public_guides(
        self, tenant: TenantContext, *, cursor: str | None, limit: int | None
    ) -> tuple[list[NeighborhoodGuide], str | None]:
        page_size = clamp_limit(limit)
        after = _decode_keyset(cursor) if cursor else None
        rows = await self.repo.list_guides(
            tenant.id, published_only=True, after=after, limit=page_size
        )
        items = rows[:page_size]
        next_cursor = _guide_cursor(items[-1]) if len(rows) > page_size else None
        return items, next_cursor

    async def get_public_guide(self, tenant: TenantContext, slug: str) -> NeighborhoodGuide:
        guide = await self.repo.get_published_guide_by_slug(tenant.id, slug)
        if guide is None:
            raise NotFoundError("Guide not found.")
        return guide

    async def guide_listings(
        self,
        tenant: TenantContext,
        guide: NeighborhoodGuide,
        *,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[Any], str | None]:
        """The published listings inside this guide's boundary — computed live
        via the listings service's ``ST_Contains`` boundary accessor (no stored
        FK). A guide with no boundary has no auto-linked listings."""
        rings = multipolygon_rings(guide.boundary)
        if not rings:
            return [], None
        return await self.listings.published_within_boundary(
            tenant.id, boundary_rings=rings, cursor=cursor, limit=limit
        )

    async def sitemap_guides(self, tenant: TenantContext) -> list[NeighborhoodGuide]:
        return await self.repo.published_guides_for_sitemap(tenant.id, limit=SITEMAP_MAX_PAGES)

    # ---- market reports: portal ----

    async def create_report(self, tenant: TenantContext, data: ReportCreate) -> MarketReport:
        report = MarketReport(
            tenant_id=tenant.id,
            slug=data.slug,
            title=data.title,
            stats=data.stats,
            status=ReportStatus.DRAFT,
        )
        self.repo.add(report)
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError("A report with this slug already exists.") from exc
        return report

    async def get_report(self, tenant: TenantContext, report_id: uuid.UUID) -> MarketReport:
        report = await self.repo.get_report(tenant.id, report_id)
        if report is None:
            raise NotFoundError("Report not found.")
        return report

    async def list_reports(
        self, tenant: TenantContext, *, cursor: str | None, limit: int | None
    ) -> tuple[list[MarketReport], str | None, int]:
        page_size = clamp_limit(limit)
        after = _decode_keyset(cursor) if cursor else None
        rows = await self.repo.list_reports(tenant.id, after=after, limit=page_size)
        items = rows[:page_size]
        next_cursor = _report_cursor(items[-1]) if len(rows) > page_size else None
        total = await self.repo.count_reports(tenant.id)
        return items, next_cursor, total

    async def update_report(
        self, tenant: TenantContext, report_id: uuid.UUID, data: ReportUpdate
    ) -> MarketReport:
        report = await self.get_report(tenant, report_id)
        for key, value in data.model_dump(exclude_unset=True).items():
            setattr(report, key, value)
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError("A report with this slug already exists.") from exc
        return report

    async def publish_report(self, tenant: TenantContext, report_id: uuid.UUID) -> MarketReport:
        """Flip to ``published`` and enqueue the PDF render post-commit. The row
        reads ``published`` (metadata live) until the worker uploads the PDF and
        flips it to ``ready`` — the gate 409s in between."""
        report = await self.repo.get_report(tenant.id, report_id, for_update=True)
        if report is None:
            raise NotFoundError("Report not found.")
        if report.status == ReportStatus.DRAFT:
            report.status = ReportStatus.PUBLISHED
            if report.published_at is None:
                report.published_at = datetime.now(UTC)
            await self.repo.flush()
            report_id_str, tenant_id_str = str(report.id), str(tenant.id)

            async def _enqueue() -> None:
                # Lazy import: the task module imports this service (for its
                # repository), so a top-level import would be circular.
                from app.workers.tasks.content import generate_report_pdf

                generate_report_pdf.delay(report_id_str, tenant_id_str)

            on_commit(self.repo.session, _enqueue)
        return report

    async def unpublish_report(self, tenant: TenantContext, report_id: uuid.UUID) -> MarketReport:
        report = await self.get_report(tenant, report_id)
        if report.status != ReportStatus.DRAFT:
            report.status = ReportStatus.DRAFT
            await self.repo.flush()
        return report

    async def delete_report(self, tenant: TenantContext, report_id: uuid.UUID) -> None:
        report = await self.get_report(tenant, report_id)
        objects = [report.pdf_object_key] if report.pdf_object_key else []
        await self.repo.delete_report(report)
        await self.repo.flush()
        if objects:
            docs_bucket = self.storage.docs_bucket

            async def _cleanup() -> None:
                from app.workers.tasks.media import delete_media_objects

                delete_media_objects.delay([[docs_bucket, key] for key in objects])

            on_commit(self.repo.session, _cleanup)

    # ---- market reports: public ----

    async def get_public_report(self, tenant: TenantContext, slug: str) -> MarketReport:
        report = await self.repo.get_published_report_by_slug(tenant.id, slug)
        if report is None:
            raise NotFoundError("Report not found.")
        return report

    async def request_report_download(
        self, tenant: TenantContext, slug: str, data: ReportDownloadCreate
    ) -> str | None:
        """The gate (§8.10): honeypot → fabricated response; otherwise mint a
        lead and hand back a short-lived presigned GET for the private PDF.
        The honeypot check runs *before* any report lookup so a bot supplying a
        bogus slug can't distinguish the honeypot path via a 404."""
        if data.hp:
            logger.info("report_download_honeypot_triggered")
            return None

        report = await self.get_public_report(tenant, slug)
        if report.status != ReportStatus.READY or not report.pdf_object_key:
            # Metadata is live but the PDF isn't rendered yet.
            raise ConflictError("This report's download is not ready yet.")

        title = pick_localized(report.title, DEFAULT_LOCALE)
        await self.leads.register_report_download_lead(
            tenant,
            data.contact,
            source_meta=_download_source_meta(data),
            report_payload={"slug": report.slug, "title": title},
        )
        return self.storage.presign_get(
            self.storage.docs_bucket,
            report.pdf_object_key,
            filename=f"report-{report.slug}.pdf",
        )


def _guide_cursor(guide: NeighborhoodGuide) -> str:
    return encode_cursor({"created_at": guide.created_at.isoformat(), "id": str(guide.id)})


def _report_cursor(report: MarketReport) -> str:
    return encode_cursor({"created_at": report.created_at.isoformat(), "id": str(report.id)})


def _download_source_meta(data: ReportDownloadCreate) -> dict[str, Any]:
    return {
        k: v
        for k, v in {
            "utm_source": data.utm_source,
            "utm_medium": data.utm_medium,
            "utm_campaign": data.utm_campaign,
            "page": data.page,
            "referrer": data.referrer,
        }.items()
        if v is not None
    }


def _decode_keyset(cursor: str) -> tuple[datetime, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(values["created_at"]), uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def get_content_service(session: SessionDep, request: Request) -> ContentService:
    return ContentService(
        ContentRepository(session),
        request.app.state.settings,
        get_listing_service(session),
        get_leads_service(session),
        request.app.state.storage,
    )


ContentServiceDep = Annotated[ContentService, Depends(get_content_service)]
