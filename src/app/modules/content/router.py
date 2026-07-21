"""HTTP layer for content CMS (§8.10, slice 1).

- ``public_router`` — the agency site: published pages (one negotiated locale)
  and current legal pages. A preview token exposes an unpublished draft.
- ``portal_router`` — the back-office: page CRUD + publish/unpublish, and
  legal-version publishing. Gated by ``CONTENT_MANAGE`` (marketing/admin).
"""

import uuid

from fastapi import APIRouter, Depends, Header, Query, status

from app.core.i18n import negotiate_locale
from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import AuthenticatedUser, Permission, require
from app.core.tenancy import TenantDep
from app.modules.content.models import LegalKind
from app.modules.content.schemas import (
    LegalIndexEntry,
    LegalPageCreate,
    LegalPageOut,
    PageCreate,
    PageOut,
    PageUpdate,
    PreviewTokenOut,
    PublicLegalPageOut,
    PublicPageOut,
)
from app.modules.content.service import ContentServiceDep

public_router = APIRouter(tags=["content:public"])


@public_router.get("/pages/{slug}")
async def get_public_page(
    slug: str,
    tenant: TenantDep,
    service: ContentServiceDep,
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> PublicPageOut:
    resolved = negotiate_locale(locale, accept_language)
    page = await service.get_public_page(tenant, slug)
    return PublicPageOut.from_page(page, resolved)


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
async def list_legal(tenant: TenantDep, service: ContentServiceDep) -> list[LegalIndexEntry]:
    rows = await service.list_current_legal(tenant)
    return [LegalIndexEntry.model_validate(r) for r in rows]


@public_router.get("/legal/{kind}")
async def get_legal(
    kind: LegalKind,
    tenant: TenantDep,
    service: ContentServiceDep,
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> PublicLegalPageOut:
    resolved = negotiate_locale(locale, accept_language)
    page = await service.get_current_legal(tenant, kind)
    return PublicLegalPageOut.from_page(page, resolved)


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
