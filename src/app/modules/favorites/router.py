"""HTTP layer for favorites & saved searches (§8.9).

- ``me_router`` — the buyer/seller dashboard surface (``/me/...``): any
  authenticated tenant account operates on its own rows. No RBAC permission —
  ownership is the authorization.
- ``public_router`` — the anonymous alert signup: rate-limited, honeypot-
  camouflaged, double-opt-in; plus the token-authorized confirm/unsubscribe.
"""

import uuid

from fastapi import APIRouter, Depends, Header, Query, status

from app.core.i18n import negotiate_locale
from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import CurrentUserDep
from app.core.rate_limit import rate_limit
from app.core.tenancy import TenantDep
from app.modules.favorites.schemas import (
    FavoriteItemOut,
    SavedSearchConfirmIn,
    SavedSearchCreate,
    SavedSearchOut,
    SavedSearchSignupIn,
    SavedSearchSignupOut,
    SavedSearchUnsubscribeIn,
    SavedSearchUpdate,
)
from app.modules.favorites.service import FavoritesServiceDep
from app.modules.listings.schemas import PublicListingOut
from app.modules.media.schemas import PublicMediaOut
from app.modules.media.service import MediaServiceDep

me_router = APIRouter(prefix="/me", tags=["favorites:me"])


@me_router.put("/favorites/{listing_id}", status_code=status.HTTP_204_NO_CONTENT)
async def add_favorite(
    listing_id: uuid.UUID,
    tenant: TenantDep,
    service: FavoritesServiceDep,
    actor: CurrentUserDep,
) -> None:
    await service.add_favorite(tenant, actor, listing_id)


@me_router.delete("/favorites/{listing_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_favorite(
    listing_id: uuid.UUID,
    tenant: TenantDep,
    service: FavoritesServiceDep,
    actor: CurrentUserDep,
) -> None:
    await service.remove_favorite(tenant, actor, listing_id)


@me_router.get("/favorites")
async def list_favorites(
    tenant: TenantDep,
    service: FavoritesServiceDep,
    media_service: MediaServiceDep,
    actor: CurrentUserDep,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> Page[FavoriteItemOut]:
    resolved = negotiate_locale(locale, accept_language)
    pairs, next_cursor = await service.list_favorites(tenant, actor, cursor=cursor, limit=limit)
    covers = await media_service.covers_for(tenant, [listing.id for _, listing in pairs])
    return Page(
        items=[
            FavoriteItemOut(
                favorited_at=favorite.created_at,
                listing=PublicListingOut.from_listing(
                    listing,
                    resolved,
                    cover=(
                        PublicMediaOut.from_media(
                            covers[listing.id], resolved, media_service.public_url
                        )
                        if listing.id in covers
                        else None
                    ),
                ),
            )
            for favorite, listing in pairs
        ],
        next_cursor=next_cursor,
    )


@me_router.post("/saved-searches", status_code=status.HTTP_201_CREATED)
async def create_saved_search(
    data: SavedSearchCreate,
    tenant: TenantDep,
    service: FavoritesServiceDep,
    actor: CurrentUserDep,
    accept_language: str | None = Header(default=None),
) -> SavedSearchOut:
    row = await service.create_saved_search(
        tenant, actor, data, fallback_locale=negotiate_locale(data.locale, accept_language)
    )
    return SavedSearchOut.from_row(row)


@me_router.get("/saved-searches")
async def list_saved_searches(
    tenant: TenantDep,
    service: FavoritesServiceDep,
    actor: CurrentUserDep,
) -> list[SavedSearchOut]:
    rows = await service.list_saved_searches(tenant, actor)
    return [SavedSearchOut.from_row(r) for r in rows]


@me_router.get("/saved-searches/{saved_search_id}")
async def get_saved_search(
    saved_search_id: uuid.UUID,
    tenant: TenantDep,
    service: FavoritesServiceDep,
    actor: CurrentUserDep,
) -> SavedSearchOut:
    return SavedSearchOut.from_row(await service.get_saved_search(tenant, actor, saved_search_id))


@me_router.patch("/saved-searches/{saved_search_id}")
async def update_saved_search(
    saved_search_id: uuid.UUID,
    data: SavedSearchUpdate,
    tenant: TenantDep,
    service: FavoritesServiceDep,
    actor: CurrentUserDep,
) -> SavedSearchOut:
    row = await service.update_saved_search(tenant, actor, saved_search_id, data)
    return SavedSearchOut.from_row(row)


@me_router.delete("/saved-searches/{saved_search_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_search(
    saved_search_id: uuid.UUID,
    tenant: TenantDep,
    service: FavoritesServiceDep,
    actor: CurrentUserDep,
) -> None:
    await service.delete_saved_search(tenant, actor, saved_search_id)


public_router = APIRouter(prefix="/saved-searches", tags=["favorites:public"])

_signup_limit = rate_limit(key_prefix="saved_search_signup", limit=5, window_seconds=60)


@public_router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(_signup_limit)])
async def signup_saved_search(
    data: SavedSearchSignupIn,
    tenant: TenantDep,
    service: FavoritesServiceDep,
    accept_language: str | None = Header(default=None),
) -> SavedSearchSignupOut:
    row = await service.signup(
        tenant, data, fallback_locale=negotiate_locale(data.locale, accept_language)
    )
    # Honeypot hits: a real-shaped id, nothing persisted (same as lead capture).
    return SavedSearchSignupOut(id=row.id if row is not None else uuid.uuid4())


@public_router.post("/confirm")
async def confirm_saved_search(
    data: SavedSearchConfirmIn,
    tenant: TenantDep,
    service: FavoritesServiceDep,
) -> SavedSearchOut:
    return SavedSearchOut.from_row(await service.confirm_signup(tenant, data.token))


@public_router.post("/unsubscribe", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe_saved_search(
    data: SavedSearchUnsubscribeIn,
    tenant: TenantDep,
    service: FavoritesServiceDep,
) -> None:
    await service.unsubscribe(tenant, data.token)
