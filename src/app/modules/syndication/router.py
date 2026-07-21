"""HTTP layer for portal syndication (§8.14).

- ``feeds_router`` — stable, auth-none, per-tenant pull feeds of published
  inventory (XML/CSV), mounted like the SEO sitemap so portals/aggregators can
  poll a discoverable URL. A live query at request time (no Celery trigger) that
  reuses the public-listing query builder.
- ``portal_router`` — ``/portal/syndication`` back-office: configure enabled
  portals, view per-listing sync state, trigger a manual re-push. Gated by
  ``LISTING_MANAGE`` (syndication is a listing concern, not a new domain).
"""

import csv
import io
import uuid
from dataclasses import dataclass
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from app.core.i18n import negotiate_locale
from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import AuthenticatedUser, Permission, require
from app.core.tenancy import TenantDep
from app.integrations.portals.registry import KNOWN_PORTALS, is_portal_enabled
from app.modules.listings.schemas import PublicListingFilters, PublicListingOut, SearchSort
from app.modules.listings.service import ListingServiceDep
from app.modules.media.service import MediaServiceDep
from app.modules.syndication.schemas import (
    PortalConfigOut,
    PortalSyncStateOut,
    SyndicationSettingsIn,
    SyndicationSettingsOut,
)
from app.modules.syndication.service import SyndicationServiceDep

# One feed caps at this many published listings (a portal that needs more can
# page the public API); keeps the pull query and response bounded.
FEED_MAX_LISTINGS = 5_000

feeds_router = APIRouter(prefix="/feeds", tags=["syndication:feeds"])


@feeds_router.get("/listings.{fmt}")
async def listings_feed(
    fmt: str,
    request: Request,
    tenant: TenantDep,
    service: ListingServiceDep,
    media_service: MediaServiceDep,
    accept_language: str | None = Header(default=None),
    locale: str | None = Query(default=None),
) -> Response:
    """Published-inventory feed for the resolved tenant host, XML or CSV.

    Pull-based (no sync trigger) — reuses the public-listing query builder with
    no filters, newest first. The tenant is resolved from the Host exactly like
    the public site, so the URL is stable per tenant."""
    if fmt not in ("xml", "csv"):
        return Response(status_code=404)
    resolved = negotiate_locale(locale, accept_language)
    items, _ = await service.list_public(
        tenant,
        filters=PublicListingFilters(),
        locale=resolved,
        sort=SearchSort.NEWEST,
        cursor=None,
        limit=FEED_MAX_LISTINGS,
    )
    covers = await media_service.covers_for(tenant, [x.id for x in items])
    host = request.headers.get("host", "").split(":")[0]

    def _cover_url(listing_id: uuid.UUID) -> str:
        cover = covers.get(listing_id)
        if cover is None or not cover.variants:
            return ""
        variant = (
            cover.variants.get("card_webp")
            or cover.variants.get("gallery_webp")
            or next(iter(cover.variants.values()))
        )
        return media_service.public_url(variant["key"])

    rows: list[_FeedRow] = [
        _FeedRow(
            out=PublicListingOut.from_listing(listing, resolved),
            cover_url=_cover_url(listing.id),
            url=f"https://{host}/listings/{listing.reference_code}",
        )
        for listing in items
    ]

    if fmt == "csv":
        return _csv_feed(rows)
    return _xml_feed(rows)


@dataclass(frozen=True, slots=True)
class _FeedRow:
    out: PublicListingOut
    cover_url: str
    url: str


def _csv_feed(rows: list[_FeedRow]) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "reference",
            "url",
            "title",
            "purpose",
            "propertyType",
            "price",
            "currency",
            "beds",
            "baths",
            "areaBuilt",
            "cover",
        ]
    )
    for row in rows:
        out = row.out
        writer.writerow(
            [
                out.reference_code,
                row.url,
                out.title or "",
                out.purpose.value,
                out.property_type.value,
                str(out.price),
                out.currency,
                out.beds if out.beds is not None else "",
                out.baths if out.baths is not None else "",
                str(out.area_built) if out.area_built is not None else "",
                row.cover_url,
            ]
        )
    return Response(content=buffer.getvalue(), media_type="text/csv")


def _xml_feed(rows: list[_FeedRow]) -> Response:
    entries = []
    for row in rows:
        out = row.out
        title = escape(out.title or out.reference_code)
        desc = escape(out.description or "")
        cover = f"<image>{escape(row.cover_url)}</image>" if row.cover_url else ""
        entries.append(
            f"<listing><reference>{escape(out.reference_code)}</reference>"
            f"<url>{escape(row.url)}</url><title>{title}</title>"
            f"<description>{desc}</description>"
            f"<purpose>{out.purpose.value}</purpose>"
            f"<propertyType>{out.property_type.value}</propertyType>"
            f"<price>{out.price}</price><currency>{out.currency}</currency>"
            f"{cover}</listing>"
        )
    xml = '<?xml version="1.0" encoding="UTF-8"?><listings>' + "".join(entries) + "</listings>"
    return Response(content=xml, media_type="application/xml")


portal_router = APIRouter(prefix="/portal/syndication", tags=["syndication:portal"])


@portal_router.get("/settings")
async def get_settings(
    tenant: TenantDep,
    service: SyndicationServiceDep,
    _: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> SyndicationSettingsOut:
    stored = await service.get_settings(tenant)
    return _settings_out(stored)


@portal_router.put("/settings")
async def put_settings(
    data: SyndicationSettingsIn,
    tenant: TenantDep,
    service: SyndicationServiceDep,
    _: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> SyndicationSettingsOut:
    stored = await service.replace_settings(tenant, data)
    return _settings_out(stored)


def _settings_out(stored: dict[str, dict[str, object]]) -> SyndicationSettingsOut:
    """Render every known portal, whether or not the tenant has configured it —
    the admin UI needs the full allowlist. Secrets are never echoed."""
    portals = []
    for key in sorted(KNOWN_PORTALS):
        config = stored.get(key) or {}
        portals.append(
            PortalConfigOut(
                key=key,
                enabled=is_portal_enabled({"syndication": stored}, key),
                base_url=(
                    str(config["base_url"]) if isinstance(config.get("base_url"), str) else None
                ),
                has_api_key=bool(config.get("api_key")),
            )
        )
    return SyndicationSettingsOut(portals=portals)


@portal_router.get("/state")
async def list_state(
    tenant: TenantDep,
    service: SyndicationServiceDep,
    _: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
    portal: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[PortalSyncStateOut]:
    items, next_cursor = await service.list_states(
        tenant, portal_key=portal, cursor=cursor, limit=limit
    )
    return Page(items=[PortalSyncStateOut.from_state(s) for s in items], next_cursor=next_cursor)


@portal_router.get("/listings/{listing_id}/state")
async def listing_state(
    listing_id: uuid.UUID,
    tenant: TenantDep,
    service: SyndicationServiceDep,
    _: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> list[PortalSyncStateOut]:
    rows = await service.state_for_listing(tenant, listing_id)
    return [PortalSyncStateOut.from_state(s) for s in rows]


@portal_router.post("/listings/{listing_id}/repush")
async def repush_listing(
    listing_id: uuid.UUID,
    tenant: TenantDep,
    service: SyndicationServiceDep,
    _: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> dict[str, list[str]]:
    portals = await service.request_repush(tenant, listing_id)
    return {"queued": portals}
