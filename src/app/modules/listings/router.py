"""HTTP layer for listings (§8.1).

- ``public_router`` — the agency site: published inventory only, one
  negotiated locale per i18n field.
- ``portal_router`` — the back-office: full CRUD, workflow transitions,
  duplication, status history. RBAC-guarded; ownership scoping happens in the
  service/repository (§7.2).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status

from app.core.i18n import negotiate_locale
from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import AuthenticatedUser, Permission, require
from app.core.tenancy import TenantDep
from app.modules.listings.models import ListingStatus
from app.modules.listings.schemas import (
    ListingCreate,
    ListingOut,
    ListingUpdate,
    PublicListingOut,
    PublicListingQuery,
    StatusHistoryOut,
    TransitionRequest,
)
from app.modules.listings.service import ListingServiceDep

public_router = APIRouter(prefix="/listings", tags=["listings:public"])


@public_router.get("")
async def list_published_listings(
    tenant: TenantDep,
    service: ListingServiceDep,
    query: Annotated[PublicListingQuery, Query()],
    accept_language: str | None = Header(default=None),
) -> Page[PublicListingOut]:
    resolved = negotiate_locale(query.locale, accept_language)
    items, next_cursor = await service.list_public(
        tenant, filters=query, cursor=query.cursor, limit=query.limit
    )
    return Page(
        items=[PublicListingOut.from_listing(x, resolved) for x in items],
        next_cursor=next_cursor,
    )


@public_router.get("/{ref_or_id}")
async def get_published_listing(
    ref_or_id: str,
    tenant: TenantDep,
    service: ListingServiceDep,
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> PublicListingOut:
    resolved = negotiate_locale(locale, accept_language)
    listing = await service.get_public(tenant, ref_or_id)
    return PublicListingOut.from_listing(listing, resolved)


portal_router = APIRouter(prefix="/portal/listings", tags=["listings:portal"])


@portal_router.post("", status_code=status.HTTP_201_CREATED)
async def create_listing(
    data: ListingCreate,
    tenant: TenantDep,
    service: ListingServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> ListingOut:
    return ListingOut.model_validate(await service.create(tenant, actor, data))


@portal_router.get("")
async def list_listings(
    tenant: TenantDep,
    service: ListingServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
    status_filter: ListingStatus | None = Query(default=None, alias="status"),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[ListingOut]:
    items, next_cursor, total = await service.list_portal(
        tenant, actor, status=status_filter, cursor=cursor, limit=limit
    )
    return Page(
        items=[ListingOut.model_validate(x) for x in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )


@portal_router.get("/{listing_id}")
async def get_listing(
    listing_id: uuid.UUID,
    tenant: TenantDep,
    service: ListingServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> ListingOut:
    return ListingOut.model_validate(await service.get_portal(tenant, actor, listing_id))


@portal_router.patch("/{listing_id}")
async def update_listing(
    listing_id: uuid.UUID,
    data: ListingUpdate,
    tenant: TenantDep,
    service: ListingServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> ListingOut:
    return ListingOut.model_validate(await service.update(tenant, actor, listing_id, data))


@portal_router.delete("/{listing_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_listing(
    listing_id: uuid.UUID,
    tenant: TenantDep,
    service: ListingServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> None:
    await service.soft_delete(tenant, actor, listing_id)


@portal_router.post("/{listing_id}/transition")
async def transition_listing(
    listing_id: uuid.UUID,
    data: TransitionRequest,
    tenant: TenantDep,
    service: ListingServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> ListingOut:
    listing = await service.transition(tenant, actor, listing_id, data.to_status)
    return ListingOut.model_validate(listing)


@portal_router.post("/{listing_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_listing(
    listing_id: uuid.UUID,
    tenant: TenantDep,
    service: ListingServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> ListingOut:
    return ListingOut.model_validate(await service.duplicate(tenant, actor, listing_id))


@portal_router.get("/{listing_id}/history")
async def listing_history(
    listing_id: uuid.UUID,
    tenant: TenantDep,
    service: ListingServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> list[StatusHistoryOut]:
    rows = await service.history(tenant, actor, listing_id)
    return [StatusHistoryOut.model_validate(r) for r in rows]
