"""HTTP layer for listings (§8.1).

- ``public_router`` — the agency site: published inventory only, one
  negotiated locale per i18n field.
- ``portal_router`` — the back-office: full CRUD, workflow transitions,
  duplication, status history. RBAC-guarded; ownership scoping happens in the
  service/repository (§7.2).
"""

import hashlib
import json
import uuid
from typing import Annotated
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from app.core.cache import cache_aside
from app.core.http_cache import cached_json_response
from app.core.i18n import negotiate_locale
from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import AuthenticatedUser, Permission, require
from app.core.tenancy import TenantDep
from app.modules.blog.service import BlogServiceDep
from app.modules.content.service import ContentServiceDep
from app.modules.listings.models import ListingStatus
from app.modules.listings.schemas import (
    GeneratedDescriptionOut,
    GenerateDescriptionRequest,
    ListingCreate,
    ListingOut,
    ListingUpdate,
    MapClusterOut,
    MapOut,
    MapPinOut,
    MapQuery,
    PublicListingOut,
    PublicListingQuery,
    StatusHistoryOut,
    TransitionRequest,
    build_json_ld,
)
from app.modules.listings.service import ListingServiceDep
from app.modules.media.schemas import MediaKind, PublicMediaOut
from app.modules.media.service import MediaServiceDep

public_router = APIRouter(prefix="/listings", tags=["listings:public"])


def _query_hash(payload: dict[str, object], locale: str) -> str:
    """A stable short digest of a query dict + locale, for a cache ident.
    ``sort_keys`` makes it order-insensitive so two equivalent query strings
    hit the same cache entry."""
    blob = json.dumps({"q": payload, "locale": locale}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@public_router.get("")
async def list_published_listings(
    tenant: TenantDep,
    service: ListingServiceDep,
    media_service: MediaServiceDep,
    query: Annotated[PublicListingQuery, Query()],
    accept_language: str | None = Header(default=None),
) -> Page[PublicListingOut]:
    resolved = negotiate_locale(query.locale, accept_language)
    items, next_cursor = await service.list_public(
        tenant,
        filters=query,
        locale=resolved,
        sort=query.sort,
        cursor=query.cursor,
        limit=query.limit,
    )
    covers = await media_service.covers_for(tenant, [x.id for x in items])
    return Page(
        items=[
            PublicListingOut.from_listing(
                x,
                resolved,
                cover=(
                    PublicMediaOut.from_media(covers[x.id], resolved, media_service.public_url)
                    if x.id in covers
                    else None
                ),
            )
            for x in items
        ],
        next_cursor=next_cursor,
    )


# Declared before /{ref_or_id} — route matching is in declaration order.
@public_router.get("/map")
async def map_published_listings(
    request: Request,
    tenant: TenantDep,
    service: ListingServiceDep,
    query: Annotated[MapQuery, Query()],
    accept_language: str | None = Header(default=None),
) -> MapOut:
    resolved = negotiate_locale(query.locale, accept_language)
    settings = request.app.state.settings

    async def _load() -> MapOut:
        pins, clusters, clustered = await service.map_points(tenant, filters=query, locale=resolved)
        return MapOut(
            clustered=clustered,
            pins=[
                MapPinOut(id=r.id, lat=r.lat, lng=r.lng, price=r.price, status=r.status)
                for r in pins
            ],
            clusters=[
                MapClusterOut(lat=float(r.lat), lng=float(r.lng), count=r.count) for r in clusters
            ],
        )

    # Keyed on a hash of the (viewport + filters) query and negotiated locale.
    # TTL-only invalidation (60s, §11): map clusters are aggregate geo data
    # where a short staleness window is acceptable, so no write-time version
    # bump is wired (unlike content pages) — the tight TTL *is* the freshness
    # guarantee.
    viewport = _query_hash(query.model_dump(mode="json", exclude_none=True), resolved)
    return await cache_aside(
        request.app.state.redis,
        tenant_id=str(tenant.id),
        entity="listing_map",
        ident=viewport,
        ttl_seconds=settings.cache_map_ttl_seconds,
        loader=_load,
        # model_dump_json encodes in one pass rather than building an
        # intermediate dict for json.dumps to walk again — map cluster
        # payloads are the largest thing cached here.
        dumps=lambda v: v.model_dump_json(),
        deserialize=MapOut.model_validate,
        enabled=settings.cache_enabled,
    )


def _jsonld_images(media: list[PublicMediaOut]) -> list[str]:
    """Largest available variant URL per photo, for the structured data."""
    urls: list[str] = []
    for item in media:
        if item.kind is not MediaKind.PHOTO:
            continue
        variant = (
            item.variants.get("full")
            or item.variants.get("gallery")
            or next(iter(item.variants.values()), None)
        )
        if variant is not None:
            urls.append(variant.url)
    return urls


@public_router.get("/{ref_or_id}")
async def get_published_listing(
    ref_or_id: str,
    request: Request,
    tenant: TenantDep,
    service: ListingServiceDep,
    media_service: MediaServiceDep,
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> Response:
    resolved = negotiate_locale(locale, accept_language)
    listing = await service.get_public(tenant, ref_or_id)
    rows = await media_service.public_for_listing(tenant, listing.id)
    media = [PublicMediaOut.from_media(m, resolved, media_service.public_url) for m in rows]
    cover = next((m for m in media if m.kind is MediaKind.PHOTO), None)
    out = PublicListingOut.from_listing(
        listing,
        resolved,
        cover=cover,
        media=media,
        json_ld=build_json_ld(listing, resolved, images=_jsonld_images(media)),
    )
    # CDN/browser validator caching (§11): a strong ETag over the body plus the
    # listing's own updated_at as Last-Modified; a matching conditional GET is a
    # 304. `s-maxage` lets an edge absorb anonymous detail-page traffic.
    return cached_json_response(
        request,
        out,
        s_maxage=request.app.state.settings.public_cache_s_maxage_seconds,
        last_modified=listing.updated_at,
    )


seo_router = APIRouter(tags=["seo"])


@seo_router.get("/sitemap.xml")
async def sitemap(
    request: Request,
    tenant: TenantDep,
    service: ListingServiceDep,
    content: ContentServiceDep,
    blog: BlogServiceDep,
) -> Response:
    """Per-tenant sitemap (§8.3/§8.10): every published listing, content page,
    blog post, and neighborhood guide, on the domain the request arrived on."""
    rows = await service.sitemap_entries(tenant)
    pages = await content.sitemap_pages(tenant)
    posts = await blog.sitemap_posts(tenant)
    guides = await content.sitemap_guides(tenant)
    host = request.headers.get("host", "").split(":")[0]
    urls = "".join(
        f"<url><loc>https://{host}/listings/{escape(row.reference_code)}</loc>"
        f"<lastmod>{row.updated_at.date().isoformat()}</lastmod></url>"
        for row in rows
    )
    urls += "".join(
        f"<url><loc>https://{host}/pages/{escape(page.slug)}</loc>"
        f"<lastmod>{page.updated_at.date().isoformat()}</lastmod></url>"
        for page in pages
    )
    urls += "".join(
        f"<url><loc>https://{host}/blog/{escape(post.slug)}</loc>"
        f"<lastmod>{post.updated_at.date().isoformat()}</lastmod></url>"
        for post in posts
    )
    urls += "".join(
        f"<url><loc>https://{host}/guides/{escape(guide.slug)}</loc>"
        f"<lastmod>{guide.updated_at.date().isoformat()}</lastmod></url>"
        for guide in guides
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )
    return Response(content=xml, media_type="application/xml")


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


@portal_router.post("/{listing_id}/generate-description")
async def generate_listing_description(
    listing_id: uuid.UUID,
    data: GenerateDescriptionRequest,
    tenant: TenantDep,
    service: ListingServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> GeneratedDescriptionOut:
    """Draft the i18n description from the listing's structured fields (§8.18).
    Returns a **draft** the agent reviews and saves via the normal PATCH — never
    auto-persisted. A provider failure surfaces as 503, not a hang."""
    description, model = await service.generate_description(tenant, actor, listing_id, data)
    return GeneratedDescriptionOut(description=description, model=model)


@portal_router.get("/{listing_id}/history")
async def listing_history(
    listing_id: uuid.UUID,
    tenant: TenantDep,
    service: ListingServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> list[StatusHistoryOut]:
    rows = await service.history(tenant, actor, listing_id)
    return [StatusHistoryOut.model_validate(r) for r in rows]
