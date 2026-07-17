"""Favorites & saved-search business logic (§8.9): idempotent favorites for
the buyer dashboard, saved searches that replay the §8.3 public filters, the
anonymous double-opt-in signup that converts into a lead, and the alert
matchers the worker tasks drive.

Ownership model: favorites and saved searches are self-owned rows — any
authenticated tenant account (buyer/renter, seller, staff alike) manages only
its own, scoped by ``user_id = actor.id`` in the repository. No permission
from the RBAC matrix is involved.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import Depends, Request
from pydantic import ValidationError
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.database import SessionDep, on_commit
from app.core.exceptions import ConflictError, NotFoundError, UnauthorizedError
from app.core.i18n import pick_localized
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.permissions import AuthenticatedUser
from app.core.security import hash_token, sign_value, unsign_value
from app.core.tenancy import TenantContext
from app.modules.favorites.models import AlertFrequency, Favorite, SavedSearch
from app.modules.favorites.repository import FavoritesRepository
from app.modules.favorites.schemas import (
    SavedSearchCreate,
    SavedSearchSignupIn,
    SavedSearchUpdate,
    dump_filters,
)
from app.modules.leads.service import LeadsService, get_leads_service
from app.modules.listings.models import Listing
from app.modules.listings.schemas import PublicListingFilters, SearchSort
from app.modules.listings.service import ListingService, get_listing_service
from app.modules.users.service import UserService, get_user_service
from app.workers.tasks.email import send_email

logger = structlog.get_logger(__name__)

# Keeps the per-publish instant matcher and the digest sweep bounded.
MAX_SAVED_SEARCHES_PER_USER = 20
# Listings shown in one digest email body.
DIGEST_MAX_LISTINGS = 10

_CONFIRM_KEY = "favorites:confirm:{}"
_UNSUBSCRIBE_PURPOSE = "saved-search-unsubscribe"

# Digests run from one daily Beat tick: daily rows every run, weekly rows on
# Mondays (§8.9). The watermark (last_run_at) is what makes reruns idempotent.
WEEKLY_DIGEST_WEEKDAY = 0


class FavoritesService:
    def __init__(
        self,
        repo: FavoritesRepository,
        listings: ListingService,
        users: UserService,
        leads: LeadsService | None = None,
        redis: Redis | None = None,
        settings: Settings | None = None,
    ) -> None:
        """``leads``/``redis``/``settings`` serve the signup token flows and
        alert emails; the /me CRUD surface works without them (mirrors
        AgentsService's optional storage deps)."""
        self.repo = repo
        self.listings = listings
        self.users = users
        self._leads = leads
        self._redis = redis
        self._settings = settings

    @property
    def leads(self) -> LeadsService:
        assert self._leads is not None, "FavoritesService was built without the leads boundary"
        return self._leads

    @property
    def redis(self) -> Redis:
        assert self._redis is not None, "FavoritesService was built without redis"
        return self._redis

    @property
    def settings(self) -> Settings:
        assert self._settings is not None, "FavoritesService was built without settings"
        return self._settings

    # ---- favorites (buyer dashboard) ----

    async def add_favorite(
        self, tenant: TenantContext, actor: AuthenticatedUser, listing_id: uuid.UUID
    ) -> None:
        # Only published inventory can be favorited — resolving through the
        # public accessor inherits the 404 for drafts/other tenants' ids.
        await self.listings.get_public(tenant, str(listing_id))
        await self.repo.upsert_favorite(tenant.id, actor.id, listing_id)

    async def remove_favorite(
        self, tenant: TenantContext, actor: AuthenticatedUser, listing_id: uuid.UUID
    ) -> None:
        await self.repo.delete_favorite(tenant.id, actor.id, listing_id)

    async def list_favorites(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        *,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[tuple[Favorite, Listing]], str | None]:
        """(favorite, published listing) pairs, newest favorite first. The
        cursor pages over *favorites*; a favorited listing that has since been
        unpublished or deleted drops out of the join (its row survives for
        when it's relisted), so a page can run short — by design."""
        page_size = clamp_limit(limit)
        after = _decode_keyset(cursor) if cursor else None
        rows = await self.repo.list_favorites(
            tenant.id, actor.id, after=after, limit=page_size
        )
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = encode_cursor(
                {"created_at": last.created_at.isoformat(), "id": str(last.id)}
            )
        listings = await self.listings.published_by_ids(
            tenant.id, [f.listing_id for f in items]
        )
        pairs = [(f, listings[f.listing_id]) for f in items if f.listing_id in listings]
        return pairs, next_cursor

    # ---- saved searches (/me) ----

    async def create_saved_search(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        data: SavedSearchCreate,
        *,
        fallback_locale: str,
    ) -> SavedSearch:
        if (
            await self.repo.count_saved_searches(tenant.id, actor.id)
            >= MAX_SAVED_SEARCHES_PER_USER
        ):
            raise ConflictError(
                f"You can keep at most {MAX_SAVED_SEARCHES_PER_USER} saved searches."
            )
        row = SavedSearch(
            tenant_id=tenant.id,
            user_id=actor.id,
            name=data.name,
            filters=dump_filters(data.filters),
            locale=data.locale or fallback_locale,
            frequency=data.frequency,
        )
        self.repo.add(row)
        await self.repo.flush()
        return row

    async def list_saved_searches(
        self, tenant: TenantContext, actor: AuthenticatedUser
    ) -> list[SavedSearch]:
        return await self.repo.list_saved_searches(tenant.id, actor.id)

    async def _get_own_or_404(
        self, tenant: TenantContext, actor: AuthenticatedUser, saved_search_id: uuid.UUID
    ) -> SavedSearch:
        row = await self.repo.get_saved_search(tenant.id, saved_search_id, user_id=actor.id)
        if row is None:
            # 404 for both "doesn't exist" and "not yours" — no existence oracle.
            raise NotFoundError("Saved search not found.")
        return row

    async def get_saved_search(
        self, tenant: TenantContext, actor: AuthenticatedUser, saved_search_id: uuid.UUID
    ) -> SavedSearch:
        return await self._get_own_or_404(tenant, actor, saved_search_id)

    async def update_saved_search(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        saved_search_id: uuid.UUID,
        data: SavedSearchUpdate,
    ) -> SavedSearch:
        row = await self._get_own_or_404(tenant, actor, saved_search_id)
        patch = data.model_dump(exclude_unset=True)
        if "filters" in patch:
            assert data.filters is not None  # reject_null_for guarantees it
            row.filters = dump_filters(data.filters)
            patch.pop("filters")
        for field, value in patch.items():
            setattr(row, field, value)
        await self.repo.flush()
        return row

    async def delete_saved_search(
        self, tenant: TenantContext, actor: AuthenticatedUser, saved_search_id: uuid.UUID
    ) -> None:
        await self._get_own_or_404(tenant, actor, saved_search_id)
        await self.repo.delete_saved_search(tenant.id, saved_search_id)

    # ---- anonymous signup + double-opt-in (§8.9) ----

    async def signup(
        self, tenant: TenantContext, data: SavedSearchSignupIn, *, fallback_locale: str
    ) -> SavedSearch | None:
        """Honeypot hits return ``None`` — the router synthesizes a real-shaped
        response and nothing persists (same stance as lead capture)."""
        if data.hp:
            logger.info("saved_search_signup_honeypot_triggered")
            return None

        row = SavedSearch(
            tenant_id=tenant.id,
            user_id=None,
            email=data.email,
            name=data.name,
            filters=dump_filters(data.filters),
            locale=data.locale or fallback_locale,
            frequency=data.frequency,
            is_active=False,  # until the emailed token is redeemed
        )
        self.repo.add(row)
        await self.repo.flush()

        token = uuid.uuid4().hex + uuid.uuid4().hex
        await self.redis.set(
            _CONFIRM_KEY.format(hash_token(token)),
            str(row.id),
            ex=self.settings.email_verification_ttl_seconds,
        )

        email_to = data.email

        async def _send_confirm() -> None:
            send_email.delay(
                to=email_to,
                subject="Confirm your search alert",
                text=(
                    "Use this code to confirm your listing alert "
                    f"subscription:\n\ncode: {token}\n"
                ),
            )

        on_commit(self.repo.session, _send_confirm)
        return row

    async def confirm_signup(self, tenant: TenantContext, token: str) -> SavedSearch:
        """GETDEL makes redemption single-use; the consumed token both
        activates the alert and creates the ``search_signup`` lead — the
        opt-in *is* the capture (§8.9)."""
        value = await self.redis.getdel(_CONFIRM_KEY.format(hash_token(token)))
        if value is None:
            raise UnauthorizedError("The confirmation token is invalid or has expired.")
        saved_search_id = uuid.UUID(value if isinstance(value, str) else value.decode())
        row = await self.repo.get_saved_search(tenant.id, saved_search_id)
        if row is None or row.email is None:
            raise UnauthorizedError("The confirmation token is invalid or has expired.")
        if not row.is_active:
            row.is_active = True
            await self.repo.flush()
            await self.leads.register_signup_lead(tenant, row.email)
        return row

    def unsubscribe_token(self, saved_search_id: uuid.UUID) -> str:
        """Stateless signed token carried in every alert email — it must not
        expire the way a Redis TTL would (§10.12: opting out has to work from
        a months-old email)."""
        return sign_value(_UNSUBSCRIBE_PURPOSE, str(saved_search_id), self.settings)

    async def unsubscribe(self, tenant: TenantContext, token: str) -> None:
        """Idempotent: unsubscribing twice, or after the search was deleted,
        succeeds silently — only a forged signature is rejected."""
        value = unsign_value(_UNSUBSCRIBE_PURPOSE, token, self.settings)
        if value is None:
            raise UnauthorizedError("The unsubscribe token is invalid.")
        try:
            saved_search_id = uuid.UUID(value)
        except ValueError as exc:
            raise UnauthorizedError("The unsubscribe token is invalid.") from exc
        row = await self.repo.get_saved_search(tenant.id, saved_search_id)
        if row is not None and row.is_active:
            row.is_active = False
            await self.repo.flush()

    # ---- alert matching (driven by workers/tasks/favorites.py) ----

    async def _recipient_for(self, tenant_id: uuid.UUID, row: SavedSearch) -> str | None:
        if row.email is not None:
            return row.email
        assert row.user_id is not None  # CHECK constraint: one owner or the other
        identity = await self.users.get_identity_if_active(tenant_id, row.user_id)
        return identity.email if identity is not None else None

    async def _parse_filters(self, row: SavedSearch) -> PublicListingFilters | None:
        """Stored filters are the validated dump, so failure means the schema
        itself moved on (e.g. a feature was retired). Deactivate rather than
        crash every future sweep on the same row."""
        try:
            return PublicListingFilters.model_validate(row.filters)
        except ValidationError:
            logger.warning("saved_search_filters_invalid", saved_search_id=str(row.id))
            row.is_active = False
            await self.repo.flush()
            return None

    async def match_published_listing(
        self, tenant: TenantContext, listing_id: uuid.UUID
    ) -> int:
        """Instant alerts: run every active ``instant`` search against one
        just-published listing; one email per match. Returns emails sent."""
        listing = await self.listings.published_by_ids(tenant.id, [listing_id])
        if listing_id not in listing:
            return 0  # unpublished again (or deleted) before the task ran
        sent = 0
        rows = await self.repo.list_active_by_frequency(tenant.id, [AlertFrequency.INSTANT])
        for row in rows:
            filters = await self._parse_filters(row)
            if filters is None:
                continue
            if not await self.listings.published_matches(
                tenant.id, listing_id, filters=filters, locale=row.locale
            ):
                continue
            recipient = await self._recipient_for(tenant.id, row)
            if recipient is None:
                continue
            self._send_alert(recipient, row, [listing[listing_id]])
            row.last_run_at = datetime.now(UTC)
            sent += 1
        await self.repo.flush()
        return sent

    async def run_digests(self, tenant: TenantContext, now: datetime) -> int:
        """Daily/weekly digests: new-since-watermark matches per search.
        ``last_run_at`` always advances (even on an empty match) — that
        watermark is the idempotency, same stance as ``flag_stale_listings``."""
        frequencies = [AlertFrequency.DAILY]
        if now.weekday() == WEEKLY_DIGEST_WEEKDAY:
            frequencies.append(AlertFrequency.WEEKLY)
        sent = 0
        rows = await self.repo.list_active_by_frequency(tenant.id, frequencies)
        for row in rows:
            filters = await self._parse_filters(row)
            if filters is None:
                continue
            # First run: only listings published since the search was created —
            # a new subscriber shouldn't get the entire back catalog.
            watermark = row.last_run_at or row.created_at
            listings, _ = await self.listings.list_public(
                tenant,
                filters=filters,
                locale=row.locale,
                sort=SearchSort.NEWEST,
                cursor=None,
                limit=DIGEST_MAX_LISTINGS,
                published_since=watermark,
            )
            row.last_run_at = now
            if not listings:
                continue
            recipient = await self._recipient_for(tenant.id, row)
            if recipient is None:
                continue
            self._send_alert(recipient, row, listings)
            sent += 1
        await self.repo.flush()
        return sent

    def _send_alert(self, recipient: str, row: SavedSearch, listings: list[Listing]) -> None:
        lines = [
            f"- {pick_localized(listing.title, row.locale) or listing.reference_code} "
            f"({listing.reference_code}) — /listings/{listing.reference_code}"
            for listing in listings
        ]
        count = len(listings)
        subject = (
            f"New listing for '{row.name}'"
            if count == 1
            else f"{count} new listings for '{row.name}'"
        )
        body = (
            f"New listings matching your saved search '{row.name}':\n\n"
            + "\n".join(lines)
            + "\n\nTo stop these alerts, use this code:"
            + f"\n\nunsubscribe: {self.unsubscribe_token(row.id)}\n"
        )
        send_email.delay(to=recipient, subject=subject, text=body)


def _decode_keyset(cursor: str) -> tuple[datetime, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(values["created_at"]), uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def get_favorites_service(session: SessionDep, request: Request) -> FavoritesService:
    return FavoritesService(
        FavoritesRepository(session),
        get_listing_service(session),
        get_user_service(session),
        leads=get_leads_service(session),
        redis=request.app.state.redis,
        settings=request.app.state.settings,
    )


FavoritesServiceDep = Annotated[FavoritesService, Depends(get_favorites_service)]
