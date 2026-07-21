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
from typing import Annotated

import structlog
from fastapi import Depends, Request
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.database import SessionDep
from app.core.exceptions import ConflictError, NotFoundError
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.security import sign_value, unsign_value
from app.core.tenancy import TenantContext
from app.modules.content.models import ContentPage, LegalKind, LegalPage, PageStatus
from app.modules.content.repository import ContentRepository
from app.modules.content.schemas import LegalPageCreate, PageCreate, PageUpdate

logger = structlog.get_logger(__name__)

_PREVIEW_PURPOSE = "page-preview"

# Content pages share the sitemap's 50k-URL budget with listings. A generous
# per-tenant page cap keeps the combined sitemap within the sitemaps.org limit
# without a full sitemap index (which arrives with a later content part).
SITEMAP_MAX_PAGES = 10_000


class ContentService:
    def __init__(self, repo: ContentRepository, settings: Settings) -> None:
        self.repo = repo
        self._settings = settings  # HMAC preview-token signing only

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


def _decode_keyset(cursor: str) -> tuple[datetime, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(values["created_at"]), uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def get_content_service(session: SessionDep, request: Request) -> ContentService:
    return ContentService(ContentRepository(session), request.app.state.settings)


ContentServiceDep = Annotated[ContentService, Depends(get_content_service)]
