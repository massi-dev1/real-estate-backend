"""HTTP layer for content CMS (§8.10).

- ``public_router`` — the agency site: published pages (one negotiated locale),
  current legal pages, neighborhood guides (with auto-linked listings), and
  gated market reports. A preview token exposes an unpublished page draft.
- ``portal_router`` — the back-office: page/guide/report CRUD + publish, and
  legal-version publishing. Gated by ``CONTENT_MANAGE`` (marketing/admin).
"""

import uuid

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from pydantic import TypeAdapter

from app.core.cache import cache_aside
from app.core.http_cache import cached_json_response
from app.core.i18n import negotiate_locale
from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import AuthenticatedUser, Permission, require
from app.core.schema import OutSchema
from app.core.tenancy import TenantDep
from app.modules.content.models import LegalKind
from app.modules.content.schemas import (
    GuideCreate,
    GuideOut,
    GuideUpdate,
    LegalIndexEntry,
    LegalPageCreate,
    LegalPageOut,
    PageCreate,
    PageOut,
    PageUpdate,
    PreviewTokenOut,
    PublicGuideOut,
    PublicLegalPageOut,
    PublicPageOut,
    PublicReportOut,
    ReportCreate,
    ReportDownloadCreate,
    ReportDownloadOut,
    ReportOut,
    ReportUpdate,
)
from app.modules.content.service import ContentServiceDep
from app.modules.listings.schemas import PublicListingOut
from app.modules.media.schemas import PublicMediaOut
from app.modules.media.service import MediaServiceDep

public_router = APIRouter(tags=["content:public"])

# Built once at import: a TypeAdapter compiles its serializer on construction,
# so building one per request would cost more than the encoding it saves.
_LEGAL_INDEX_ADAPTER = TypeAdapter(list[LegalIndexEntry])


class PublicGuideDetailOut(OutSchema):
    """The guide detail: the guide plus a page of listings inside its boundary
    (auto-linked live via ``ST_Contains``) and the paging cursor for more."""

    guide: PublicGuideOut
    listings: list[PublicListingOut]
    listings_next_cursor: str | None = None


@public_router.get("/pages/{slug}")
async def get_public_page(
    slug: str,
    request: Request,
    tenant: TenantDep,
    service: ContentServiceDep,
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> Response:
    resolved = negotiate_locale(locale, accept_language)
    settings = request.app.state.settings

    async def _load() -> PublicPageOut:
        page = await service.get_public_page(tenant, slug)
        return PublicPageOut.from_page(page, resolved)

    out = await cache_aside(
        request.app.state.redis,
        tenant_id=str(tenant.id),
        entity="content_page",
        ident=f"{slug}:{resolved}",
        ttl_seconds=settings.cache_content_ttl_seconds,
        loader=_load,
        # One-pass encode (see cache_aside's `dumps`) — no intermediate dict.
        dumps=lambda v: v.model_dump_json(),
        deserialize=PublicPageOut.model_validate,
        enabled=settings.cache_enabled,
    )
    # Redis absorbs origin load; ETag + Cache-Control let the CDN/browser
    # absorb the rest (§11).
    return cached_json_response(request, out, s_maxage=settings.public_cache_s_maxage_seconds)


@public_router.get("/pages/{slug}/preview")
async def preview_page(
    slug: str,
    token: str,
    tenant: TenantDep,
    service: ContentServiceDep,
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> PublicPageOut:
    resolved = negotiate_locale(locale, accept_language)
    page = await service.get_preview_page(tenant, slug, token)
    return PublicPageOut.from_page(page, resolved)


@public_router.get("/legal")
async def list_legal(
    request: Request, tenant: TenantDep, service: ContentServiceDep
) -> list[LegalIndexEntry]:
    settings = request.app.state.settings

    async def _load() -> list[LegalIndexEntry]:
        rows = await service.list_current_legal(tenant)
        return [LegalIndexEntry.model_validate(r) for r in rows]

    return await cache_aside(
        request.app.state.redis,
        tenant_id=str(tenant.id),
        entity="legal",
        ident="index",
        ttl_seconds=settings.cache_content_ttl_seconds,
        loader=_load,
        # TypeAdapter encodes the whole list in one pass, rather than dumping
        # each entry to a dict and re-walking them all in json.dumps.
        dumps=_LEGAL_INDEX_ADAPTER.dump_json,
        deserialize=lambda raw: [LegalIndexEntry.model_validate(e) for e in raw],
        enabled=settings.cache_enabled,
    )


@public_router.get("/legal/{kind}")
async def get_legal(
    kind: LegalKind,
    request: Request,
    tenant: TenantDep,
    service: ContentServiceDep,
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> Response:
    resolved = negotiate_locale(locale, accept_language)
    settings = request.app.state.settings

    async def _load() -> PublicLegalPageOut:
        page = await service.get_current_legal(tenant, kind)
        return PublicLegalPageOut.from_page(page, resolved)

    out = await cache_aside(
        request.app.state.redis,
        tenant_id=str(tenant.id),
        entity="legal",
        ident=f"{kind.value}:{resolved}",
        ttl_seconds=settings.cache_content_ttl_seconds,
        loader=_load,
        dumps=lambda v: v.model_dump_json(),
        deserialize=PublicLegalPageOut.model_validate,
        enabled=settings.cache_enabled,
    )
    return cached_json_response(request, out, s_maxage=settings.public_cache_s_maxage_seconds)


# ---- neighborhood guides (public) ----


@public_router.get("/guides")
async def list_public_guides(
    tenant: TenantDep,
    service: ContentServiceDep,
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[PublicGuideOut]:
    resolved = negotiate_locale(locale, accept_language)
    items, next_cursor = await service.list_public_guides(tenant, cursor=cursor, limit=limit)
    return Page(
        items=[PublicGuideOut.from_guide(g, resolved) for g in items],
        next_cursor=next_cursor,
    )


@public_router.get("/guides/{slug}")
async def get_public_guide(
    slug: str,
    tenant: TenantDep,
    service: ContentServiceDep,
    media_service: MediaServiceDep,
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> PublicGuideDetailOut:
    resolved = negotiate_locale(locale, accept_language)
    guide = await service.get_public_guide(tenant, slug)
    listings, listings_cursor = await service.guide_listings(
        tenant, guide, cursor=cursor, limit=limit
    )
    covers = await media_service.covers_for(tenant, [x.id for x in listings])
    return PublicGuideDetailOut(
        guide=PublicGuideOut.from_guide(guide, resolved),
        listings=[
            PublicListingOut.from_listing(
                x,
                resolved,
                cover=(
                    PublicMediaOut.from_media(covers[x.id], resolved, media_service.public_url)
                    if x.id in covers
                    else None
                ),
            )
            for x in listings
        ],
        listings_next_cursor=listings_cursor,
    )


# ---- market reports (public) ----


@public_router.get("/reports/{slug}")
async def get_public_report(
    slug: str,
    tenant: TenantDep,
    service: ContentServiceDep,
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> PublicReportOut:
    resolved = negotiate_locale(locale, accept_language)
    report = await service.get_public_report(tenant, slug)
    return PublicReportOut.from_report(report, resolved)


@public_router.post("/reports/{slug}/download")
async def download_report(
    slug: str,
    data: ReportDownloadCreate,
    tenant: TenantDep,
    service: ContentServiceDep,
) -> ReportDownloadOut:
    """The gate (§8.10 "email required to download → lead"): mints a lead and
    returns a short-lived presigned GET. A honeypot hit gets a real-shaped
    response with a dummy URL — nothing persists, nothing distinguishes it."""
    url = await service.request_report_download(tenant, slug, data)
    # Honeypot camouflage: the service returns None; hand back a real-shaped
    # response so a bot sees no difference from a genuine submission.
    return ReportDownloadOut(download_url=url or "https://example.invalid/")


portal_router = APIRouter(prefix="/portal/content", tags=["content:portal"])


@portal_router.post("/pages", status_code=status.HTTP_201_CREATED)
async def create_page(
    data: PageCreate,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> PageOut:
    return PageOut.model_validate(await service.create_page(tenant, data))


@portal_router.get("/pages")
async def list_pages(
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[PageOut]:
    items, next_cursor, total = await service.list_pages(tenant, cursor=cursor, limit=limit)
    return Page(
        items=[PageOut.model_validate(x) for x in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )


@portal_router.get("/pages/{page_id}")
async def get_page(
    page_id: uuid.UUID,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> PageOut:
    return PageOut.model_validate(await service.get_page(tenant, page_id))


@portal_router.patch("/pages/{page_id}")
async def update_page(
    page_id: uuid.UUID,
    data: PageUpdate,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> PageOut:
    return PageOut.model_validate(await service.update_page(tenant, page_id, data))


@portal_router.delete("/pages/{page_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_page(
    page_id: uuid.UUID,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> None:
    await service.delete_page(tenant, page_id)


@portal_router.post("/pages/{page_id}/publish")
async def publish_page(
    page_id: uuid.UUID,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> PageOut:
    return PageOut.model_validate(await service.publish_page(tenant, page_id))


@portal_router.post("/pages/{page_id}/unpublish")
async def unpublish_page(
    page_id: uuid.UUID,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> PageOut:
    return PageOut.model_validate(await service.unpublish_page(tenant, page_id))


@portal_router.post("/pages/{page_id}/preview-token")
async def mint_preview_token(
    page_id: uuid.UUID,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> PreviewTokenOut:
    page = await service.get_page(tenant, page_id)
    return PreviewTokenOut(token=service.preview_token(tenant, page))


@portal_router.get("/legal")
async def list_legal_current(
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> list[LegalPageOut]:
    rows = await service.list_current_legal(tenant)
    return [LegalPageOut.model_validate(r) for r in rows]


@portal_router.get("/legal/{kind}/history")
async def legal_history(
    kind: LegalKind,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> list[LegalPageOut]:
    rows = await service.list_legal_history(tenant, kind)
    return [LegalPageOut.model_validate(r) for r in rows]


@portal_router.post("/legal", status_code=status.HTTP_201_CREATED)
async def publish_legal(
    data: LegalPageCreate,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> LegalPageOut:
    return LegalPageOut.model_validate(await service.publish_legal_version(tenant, data))


# ---- neighborhood guides (portal) ----


@portal_router.post("/guides", status_code=status.HTTP_201_CREATED)
async def create_guide(
    data: GuideCreate,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> GuideOut:
    return GuideOut.model_validate(await service.create_guide(tenant, data))


@portal_router.get("/guides")
async def list_guides(
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[GuideOut]:
    items, next_cursor, total = await service.list_guides(tenant, cursor=cursor, limit=limit)
    return Page(
        items=[GuideOut.model_validate(x) for x in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )


@portal_router.get("/guides/{guide_id}")
async def get_guide(
    guide_id: uuid.UUID,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> GuideOut:
    return GuideOut.model_validate(await service.get_guide(tenant, guide_id))


@portal_router.patch("/guides/{guide_id}")
async def update_guide(
    guide_id: uuid.UUID,
    data: GuideUpdate,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> GuideOut:
    return GuideOut.model_validate(await service.update_guide(tenant, guide_id, data))


@portal_router.delete("/guides/{guide_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_guide(
    guide_id: uuid.UUID,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> None:
    await service.delete_guide(tenant, guide_id)


@portal_router.post("/guides/{guide_id}/publish")
async def publish_guide(
    guide_id: uuid.UUID,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> GuideOut:
    return GuideOut.model_validate(await service.publish_guide(tenant, guide_id))


@portal_router.post("/guides/{guide_id}/unpublish")
async def unpublish_guide(
    guide_id: uuid.UUID,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> GuideOut:
    return GuideOut.model_validate(await service.unpublish_guide(tenant, guide_id))


# ---- market reports (portal) ----


@portal_router.post("/reports", status_code=status.HTTP_201_CREATED)
async def create_report(
    data: ReportCreate,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> ReportOut:
    return ReportOut.model_validate(await service.create_report(tenant, data))


@portal_router.get("/reports")
async def list_reports(
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[ReportOut]:
    items, next_cursor, total = await service.list_reports(tenant, cursor=cursor, limit=limit)
    return Page(
        items=[ReportOut.model_validate(x) for x in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )


@portal_router.get("/reports/{report_id}")
async def get_report(
    report_id: uuid.UUID,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> ReportOut:
    return ReportOut.model_validate(await service.get_report(tenant, report_id))


@portal_router.patch("/reports/{report_id}")
async def update_report(
    report_id: uuid.UUID,
    data: ReportUpdate,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> ReportOut:
    return ReportOut.model_validate(await service.update_report(tenant, report_id, data))


@portal_router.delete("/reports/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_report(
    report_id: uuid.UUID,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> None:
    await service.delete_report(tenant, report_id)


@portal_router.post("/reports/{report_id}/publish")
async def publish_report(
    report_id: uuid.UUID,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> ReportOut:
    return ReportOut.model_validate(await service.publish_report(tenant, report_id))


@portal_router.post("/reports/{report_id}/unpublish")
async def unpublish_report(
    report_id: uuid.UUID,
    tenant: TenantDep,
    service: ContentServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> ReportOut:
    return ReportOut.model_validate(await service.unpublish_report(tenant, report_id))
