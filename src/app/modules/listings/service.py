"""Listings business logic: reference codes, ownership scoping, the publishing
workflow (§8.1), and locale-negotiated public output.

Scoping model (§7.2): agents act on listings they own (assigned or created);
team leads, admins and marketing act tenant-wide. Publishing additionally
needs ``LISTING_PUBLISH`` — or, for agents, the tenant's
``settings.listings.agent_self_publish`` flag.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

import structlog
from fastapi import Depends
from sqlalchemy import Row

from app.core.database import SessionDep
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.permissions import AuthenticatedUser, Permission, Role
from app.core.tenancy import TenantContext
from app.modules.listings.geo import to_point
from app.modules.listings.models import (
    Listing,
    ListingStatus,
    ListingStatusHistory,
)
from app.modules.listings.repository import ListingRepository, PublicKeyset
from app.modules.listings.schemas import (
    PURPOSE_PRICE_PERIOD,
    ListingCreate,
    ListingUpdate,
    PublicListingFilters,
    SearchSort,
)
from app.modules.users.service import UserService, get_user_service

logger = structlog.get_logger(__name__)

# Above this many hits in a map viewport, pins collapse into geohash-bucket
# clusters (§8.3) — the payload stays bounded no matter the zoom level.
MAP_PIN_LIMIT = 500

# One sitemap file caps at 50k URLs (sitemaps.org); a sitemap index arrives
# with the content part if any tenant outgrows this.
SITEMAP_MAX_URLS = 50_000

# Roles whose LISTING_MANAGE covers the whole tenant, not just their own rows.
MANAGES_ALL_ROLES = frozenset({Role.ADMIN, Role.TEAM_LEAD, Role.MARKETING})

# The workflow graph (§8.1). `archived → draft` is the relist path.
ALLOWED_TRANSITIONS: dict[ListingStatus, frozenset[ListingStatus]] = {
    ListingStatus.DRAFT: frozenset(
        {ListingStatus.REVIEW, ListingStatus.PUBLISHED, ListingStatus.ARCHIVED}
    ),
    ListingStatus.REVIEW: frozenset(
        {ListingStatus.DRAFT, ListingStatus.PUBLISHED, ListingStatus.ARCHIVED}
    ),
    ListingStatus.PUBLISHED: frozenset(
        {ListingStatus.RESERVED, ListingStatus.SOLD, ListingStatus.RENTED, ListingStatus.ARCHIVED}
    ),
    ListingStatus.RESERVED: frozenset(
        {ListingStatus.PUBLISHED, ListingStatus.SOLD, ListingStatus.RENTED, ListingStatus.ARCHIVED}
    ),
    ListingStatus.SOLD: frozenset({ListingStatus.ARCHIVED}),
    ListingStatus.RENTED: frozenset({ListingStatus.ARCHIVED}),
    ListingStatus.ARCHIVED: frozenset({ListingStatus.DRAFT}),
}

# States a listing must leave (via the workflow) before it can be deleted.
UNDELETABLE_STATUSES = frozenset(
    {ListingStatus.PUBLISHED, ListingStatus.RESERVED, ListingStatus.SOLD, ListingStatus.RENTED}
)


def _reference_prefix(slug: str) -> str:
    letters = "".join(ch for ch in slug if ch.isalnum())[:3].upper()
    return letters or "LST"


class ListingService:
    def __init__(self, repo: ListingRepository, users: UserService) -> None:
        self.repo = repo
        self.users = users

    # ---- scoping helpers ----

    @staticmethod
    def _scope_for(actor: AuthenticatedUser) -> uuid.UUID | None:
        """``None`` means tenant-wide; a user id narrows to owned listings."""
        return None if actor.role in MANAGES_ALL_ROLES else actor.id

    async def _get_scoped_or_404(
        self,
        tenant_id: uuid.UUID,
        actor: AuthenticatedUser,
        listing_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> Listing:
        listing = await self.repo.get(
            tenant_id, listing_id, scope_user_id=self._scope_for(actor), for_update=for_update
        )
        if listing is None:
            # 404 for both "doesn't exist" and "not yours" — no existence oracle.
            raise NotFoundError("Listing not found.")
        return listing

    async def _resolve_agent(
        self, tenant_id: uuid.UUID, actor: AuthenticatedUser, agent_id: uuid.UUID | None
    ) -> uuid.UUID:
        """Agents always list under themselves; broader roles may assign any
        active tenant account."""
        if agent_id is None or agent_id == actor.id:
            return actor.id
        if actor.role not in MANAGES_ALL_ROLES:
            raise PermissionDeniedError("You can only create listings assigned to yourself.")
        identity = await self.users.get_identity_if_active(tenant_id, agent_id)
        if identity is None:
            raise ConflictError("The assigned agent does not exist or is not active.")
        return agent_id

    def _can_publish(self, actor: AuthenticatedUser, tenant: TenantContext) -> bool:
        if actor.has_permission(Permission.LISTING_PUBLISH):
            return True
        listings_settings: dict[str, Any] = tenant.settings.get("listings") or {}
        return actor.role is Role.AGENT and bool(listings_settings.get("agent_self_publish"))

    # ---- CRUD ----

    async def create(
        self, tenant: TenantContext, actor: AuthenticatedUser, data: ListingCreate
    ) -> Listing:
        agent_id = await self._resolve_agent(tenant.id, actor, data.agent_id)
        year = datetime.now(UTC).year
        number = await self.repo.next_reference_number(tenant.id, year)
        listing = Listing(
            tenant_id=tenant.id,
            reference_code=f"{_reference_prefix(tenant.slug)}-{year}-{number:05d}",
            agent_id=agent_id,
            purpose=data.purpose,
            property_type=data.property_type,
            title=data.title,
            description=data.description or {},
            price=data.price,
            currency=data.currency,
            price_period=PURPOSE_PRICE_PERIOD[data.purpose],
            negotiable=data.negotiable,
            beds=data.beds,
            baths=data.baths,
            area_built=data.area_built,
            area_land=data.area_land,
            floor=data.floor,
            floors_total=data.floors_total,
            year_built=data.year_built,
            features=data.features,
            address=data.address.model_dump(exclude_none=True) if data.address else {},
            location=to_point(data.location.lat, data.location.lng) if data.location else None,
            expires_at=data.expires_at,
            created_by=actor.id,
        )
        self.repo.add(listing)
        await self.repo.flush()
        return listing

    async def update(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        listing_id: uuid.UUID,
        data: ListingUpdate,
    ) -> Listing:
        listing = await self._get_scoped_or_404(tenant.id, actor, listing_id)
        patch = data.model_dump(exclude_unset=True)
        if "featured" in patch and actor.role not in MANAGES_ALL_ROLES:
            # Paid placement (§8.3) is an agency-level decision, not an
            # agent's self-service toggle.
            raise PermissionDeniedError("Only managers can feature a listing.")
        if "agent_id" in patch:
            if data.agent_id is None:
                # Explicit null = unassign — must not fall into _resolve_agent's
                # create-time "default to self" (review finding: an admin
                # clearing a departed agent got the listing themselves).
                if actor.role not in MANAGES_ALL_ROLES:
                    raise PermissionDeniedError("Only managers can unassign a listing's agent.")
                listing.agent_id = None
            else:
                listing.agent_id = await self._resolve_agent(tenant.id, actor, data.agent_id)
            del patch["agent_id"]
        if "address" in patch:
            listing.address = data.address.model_dump(exclude_none=True) if data.address else {}
            del patch["address"]
        if "location" in patch:
            listing.location = (
                to_point(data.location.lat, data.location.lng) if data.location else None
            )
            del patch["location"]
        for field, value in patch.items():
            setattr(listing, field, value)
        if patch and listing.stale_flagged_at is not None:
            # An edit is exactly the "agent review" the flag was raising (§8.1).
            listing.stale_flagged_at = None
        await self.repo.flush()
        return listing

    async def get_portal(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        listing_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> Listing:
        """``for_update`` lets dependent modules (media quota checks) serialize
        their read-validate-write flows on the listing row."""
        return await self._get_scoped_or_404(tenant.id, actor, listing_id, for_update=for_update)

    async def list_portal(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        *,
        status: ListingStatus | None,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[Listing], str | None, int]:
        page_size = clamp_limit(limit)
        after = _decode_keyset(cursor, "created_at") if cursor else None
        scope = self._scope_for(actor)
        rows = await self.repo.list_portal(
            tenant.id, scope_user_id=scope, status=status, after=after, limit=page_size
        )
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = encode_cursor(
                {"created_at": last.created_at.isoformat(), "id": str(last.id)}
            )
        total = await self.repo.count(tenant.id, scope_user_id=scope, status=status)
        return items, next_cursor, total

    async def soft_delete(
        self, tenant: TenantContext, actor: AuthenticatedUser, listing_id: uuid.UUID
    ) -> None:
        listing = await self._get_scoped_or_404(tenant.id, actor, listing_id, for_update=True)
        if listing.status in UNDELETABLE_STATUSES:
            raise ConflictError("Archive this listing before deleting it.")
        listing.deleted_at = datetime.now(UTC)
        await self.repo.flush()

    async def duplicate(
        self, tenant: TenantContext, actor: AuthenticatedUser, listing_id: uuid.UUID
    ) -> Listing:
        """New draft with a fresh reference code; media (a later part) is
        deliberately not copied."""
        source = await self._get_scoped_or_404(tenant.id, actor, listing_id)
        year = datetime.now(UTC).year
        number = await self.repo.next_reference_number(tenant.id, year)
        copy = Listing(
            tenant_id=tenant.id,
            reference_code=f"{_reference_prefix(tenant.slug)}-{year}-{number:05d}",
            agent_id=source.agent_id,
            purpose=source.purpose,
            property_type=source.property_type,
            title=dict(source.title),
            description=dict(source.description),
            price=source.price,
            currency=source.currency,
            price_period=source.price_period,
            negotiable=source.negotiable,
            beds=source.beds,
            baths=source.baths,
            area_built=source.area_built,
            area_land=source.area_land,
            floor=source.floor,
            floors_total=source.floors_total,
            year_built=source.year_built,
            features=list(source.features),
            address=dict(source.address),
            location=source.location,
            created_by=actor.id,
        )
        self.repo.add(copy)
        await self.repo.flush()
        return copy

    # ---- workflow ----

    async def transition(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        listing_id: uuid.UUID,
        to_status: ListingStatus,
    ) -> Listing:
        # Row lock: two concurrent transitions must serialize and re-validate,
        # not both leave the same stale status (review finding — lost update +
        # a history trail claiming two departures from one state).
        listing = await self._get_scoped_or_404(tenant.id, actor, listing_id, for_update=True)
        if to_status not in ALLOWED_TRANSITIONS[listing.status]:
            raise ConflictError(
                f"A listing cannot go from '{listing.status.value}' to '{to_status.value}'."
            )
        if to_status is ListingStatus.PUBLISHED and not self._can_publish(actor, tenant):
            raise PermissionDeniedError(
                "Publishing requires review by someone with publish rights."
            )
        from_status = listing.status
        listing.status = to_status
        if to_status is ListingStatus.PUBLISHED:
            listing.published_at = datetime.now(UTC)
        else:
            listing.stale_flagged_at = None
        self.repo.add(
            ListingStatusHistory(
                tenant_id=tenant.id,
                listing_id=listing.id,
                from_status=from_status,
                to_status=to_status,
                changed_by=actor.id,
            )
        )
        await self.repo.flush()
        if to_status is ListingStatus.PUBLISHED:
            # Domain event consumers (search index, saved-search alerts,
            # portal syndication) arrive with the outbox in a later part.
            logger.info(
                "listing_published",
                listing_id=str(listing.id),
                reference_code=listing.reference_code,
            )
        return listing

    async def history(
        self, tenant: TenantContext, actor: AuthenticatedUser, listing_id: uuid.UUID
    ) -> list[ListingStatusHistory]:
        await self._get_scoped_or_404(tenant.id, actor, listing_id)
        return await self.repo.history(tenant.id, listing_id)

    # ---- public site ----

    async def get_public(self, tenant: TenantContext, ref_or_id: str) -> Listing:
        listing = await self.repo.get_published_by_ref_or_id(tenant.id, ref_or_id)
        if listing is None:
            raise NotFoundError("Listing not found.")
        return listing

    async def list_public(
        self,
        tenant: TenantContext,
        *,
        filters: PublicListingFilters,
        locale: str,
        sort: SearchSort,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[Listing], str | None]:
        page_size = clamp_limit(limit)
        after = _decode_public_cursor(cursor, sort) if cursor else None
        rows = await self.repo.list_published(
            tenant.id, filters=filters, locale=locale, sort=sort, after=after, limit=page_size
        )
        items = rows[:page_size]
        next_cursor = _encode_public_cursor(items[-1], sort) if len(rows) > page_size else None
        return items, next_cursor

    async def map_points(
        self, tenant: TenantContext, *, filters: PublicListingFilters, locale: str
    ) -> tuple[list[Row[Any]], list[Row[Any]], bool]:
        """(pins, clusters, clustered) — pins up to ``MAP_PIN_LIMIT`` hits in
        the viewport, geohash clusters beyond it (§8.3)."""
        total = await self.repo.count_mappable(tenant.id, filters=filters, locale=locale)
        if total <= MAP_PIN_LIMIT:
            pins = await self.repo.map_pins(
                tenant.id, filters=filters, locale=locale, limit=MAP_PIN_LIMIT
            )
            return pins, [], False
        clusters = await self.repo.map_clusters(
            tenant.id, filters=filters, locale=locale, precision=_cluster_precision(filters.bbox)
        )
        return [], clusters, True

    async def sitemap_entries(self, tenant: TenantContext) -> list[Row[Any]]:
        """(reference_code, updated_at) of every published listing."""
        return await self.repo.sitemap_rows(tenant.id, limit=SITEMAP_MAX_URLS)


def _cluster_precision(bbox: tuple[float, float, float, float] | None) -> int:
    """Geohash precision matched to the viewport: wider view → coarser
    buckets. Without a viewport, city-scale buckets (~20 km)."""
    if bbox is None:
        return 4
    span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    if span >= 20:
        return 2
    if span >= 5:
        return 3
    if span >= 1:
        return 4
    if span >= 0.2:
        return 5
    return 6


def _encode_public_cursor(last: Listing, sort: SearchSort) -> str:
    if sort is SearchSort.NEWEST:
        assert last.published_at is not None  # published rows always carry it
        key = last.published_at.isoformat()
    elif sort in (SearchSort.PRICE_ASC, SearchSort.PRICE_DESC):
        key = str(last.price)
    else:
        key = str(last.area_built or 0)  # matches the coalesce(area_built, 0) key
    return encode_cursor(
        {"sort": sort.value, "featured": last.featured, "key": key, "id": str(last.id)}
    )


def _decode_public_cursor(cursor: str, sort: SearchSort) -> PublicKeyset:
    values = decode_cursor(cursor)
    try:
        if values["sort"] != sort.value:
            # A cursor only orders the sort it was minted under.
            raise InvalidCursorError("The cursor does not match the requested sort.")
        key: Any = (
            datetime.fromisoformat(values["key"])
            if sort is SearchSort.NEWEST
            else Decimal(values["key"])
        )
        return bool(values["featured"]), key, uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def _decode_keyset(cursor: str, ts_field: str) -> tuple[datetime, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(values[ts_field]), uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def get_listing_service(session: SessionDep) -> ListingService:
    return ListingService(ListingRepository(session), get_user_service(session))


ListingServiceDep = Annotated[ListingService, Depends(get_listing_service)]
