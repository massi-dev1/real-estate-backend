"""Portal syndication business logic (§8.14).

Two concerns live here:

1. **Admin config + visibility** (request-time, portal router): read/replace the
   tenant's ``settings.syndication`` namespace (through the tenants boundary, so
   syndication never touches the tenants table), list per-listing sync state,
   and trigger a manual re-push.
2. **Sync execution** (worker-time, called by the ``sync`` Celery task): resolve
   the enabled adapters for a tenant, build the portal payload through the
   listings + media boundaries, invoke the adapter verb, and record the outcome
   on ``portal_sync_state`` — including the **circuit breaker** that pauses a
   portal-tenant pair after ``CIRCUIT_BREAKER_THRESHOLD`` consecutive failures so
   one broken portal can't retry-storm forever.

The service holds no adapter I/O of its own — adapters come from the
``integrations.portals`` registry, keyed off tenant settings. Whether a given
attempt should be *retried* by Celery is signalled back to the task via the
:class:`SyncOutcome` it returns (``retry`` = transient failure, circuit still
closed); the task raises to trigger Celery's backoff only then.
"""

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import SessionDep, on_commit
from app.core.exceptions import ConflictError, NotFoundError
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.storage import ObjectStorage, create_storage
from app.core.tenancy import TenantContext
from app.integrations.portals.base import (
    PortalAction,
    PortalAdapter,
    PortalError,
    PortalListing,
    PortalResult,
)
from app.integrations.portals.registry import (
    KNOWN_PORTALS,
    build_adapter,
    enabled_portal_keys,
)
from app.modules.listings.service import ListingService, get_listing_service
from app.modules.media.service import MediaService, build_media_boundary
from app.modules.syndication.models import PortalSyncState, SyncStatus
from app.modules.syndication.repository import SyndicationRepository
from app.modules.syndication.schemas import SyndicationSettingsIn
from app.modules.tenants.service import TenantService, build_tenant_boundary

logger = structlog.get_logger(__name__)

SETTINGS_KEY = "syndication"

# Consecutive failures on one portal-tenant pair before the breaker opens and
# syncing is paused (surfaced in the portal UI as ``circuit_open``). A v1 breaker
# — no half-open probing; an admin's manual re-push resets it.
CIRCUIT_BREAKER_THRESHOLD = 5

# Retry ceiling handed to Celery for a transient failure (backoff is Celery's).
MAX_SYNC_RETRIES = 6


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """What the task should do next. ``retry`` is True only for a transient
    failure while the circuit is still closed — the task raises to let Celery
    back off. A permanent failure, a success, a paused circuit, or "nothing to
    do" all return ``retry=False`` (terminal for this delivery)."""

    status: str
    retry: bool = False
    detail: str = ""


class SyndicationService:
    def __init__(
        self,
        repo: SyndicationRepository,
        listings: ListingService,
        media: MediaService,
        tenants: TenantService,
    ) -> None:
        self.repo = repo
        self.listings = listings
        self.media = media
        self.tenants = tenants

    # ---- admin config ----

    async def get_settings(self, tenant: TenantContext) -> dict[str, dict[str, object]]:
        """The tenant's ``settings.syndication`` namespace (raw), for the admin
        surface to render every known portal's config against."""
        return await self.tenants.get_settings_key(tenant.id, SETTINGS_KEY)  # type: ignore[return-value]

    async def replace_settings(
        self, tenant: TenantContext, data: SyndicationSettingsIn
    ) -> dict[str, dict[str, object]]:
        """Replace the whole ``settings.syndication`` namespace. Keys are already
        validated against ``KNOWN_PORTALS`` by the schema."""
        namespace: dict[str, object] = {
            key: cfg.model_dump(exclude_none=True) for key, cfg in data.portals.items()
        }
        await self.tenants.replace_settings_key(tenant.id, SETTINGS_KEY, namespace)
        return await self.tenants.get_settings_key(tenant.id, SETTINGS_KEY)  # type: ignore[return-value]

    # ---- admin visibility ----

    async def state_for_listing(
        self, tenant: TenantContext, listing_id: uuid.UUID
    ) -> list[PortalSyncState]:
        return await self.repo.for_listing(tenant.id, listing_id)

    async def list_states(
        self,
        tenant: TenantContext,
        *,
        portal_key: str | None,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[PortalSyncState], str | None]:
        page_size = clamp_limit(limit)
        after = _decode_cursor(cursor) if cursor else None
        rows = await self.repo.list_page(
            tenant.id, portal_key=portal_key, after=after, limit=page_size
        )
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = encode_cursor(
                {"updated_at": last.updated_at.isoformat(), "id": str(last.id)}
            )
        return items, next_cursor

    # ---- manual re-push (admin action) ----

    async def request_repush(self, tenant: TenantContext, listing_id: uuid.UUID) -> list[str]:
        """Admin "re-push this listing now". Resets any open circuit for the
        listing's portals and enqueues a fresh sync per enabled portal. Returns
        the portal keys queued. 404 if the listing isn't a currently-published
        one this tenant owns (no oracle — same stance as the rest of listings)."""
        payload = await self.listings.portal_payload_for(tenant.id, listing_id)
        if payload is None:
            raise NotFoundError("Published listing not found.")
        portals = enabled_portal_keys(tenant.settings)
        if not portals:
            raise ConflictError("No portals are enabled for this tenant.")
        for portal_key in portals:
            # Clear a tripped breaker so the manual action actually retries.
            state = await self.repo.get(tenant.id, listing_id, portal_key, for_update=True)
            if state is not None and state.circuit_open:
                state.circuit_open = False
                state.consecutive_failures = 0
            await self.repo.flush()
            _enqueue_sync(self.repo, tenant.id, listing_id, portal_key, PortalAction.PUSH)
        return portals

    # ---- sync execution (worker) ----

    async def sync_to_portal(
        self,
        tenant: TenantContext,
        listing_id: uuid.UUID,
        portal_key: str,
        action: PortalAction,
    ) -> SyncOutcome:
        """Run one portal sync for one listing and record the outcome. Called by
        the ``sync`` Celery task inside a tenant-scoped transaction."""
        if portal_key not in KNOWN_PORTALS:
            return SyncOutcome(status="skipped", detail="unknown portal")

        state = await self._state_row(tenant.id, listing_id, portal_key)
        if state.circuit_open:
            # Breaker tripped — do not even attempt (no retry-storm, §8.14).
            return SyncOutcome(status="paused", detail="circuit open")

        adapter = build_adapter(tenant.settings, portal_key)
        if adapter is None:
            # Portal disabled/misconfigured since the event was enqueued.
            state.last_status = SyncStatus.PENDING
            return SyncOutcome(status="skipped", detail="portal not enabled")

        # Resolve the effective action: a publish/update on a listing never
        # pushed becomes a push; a remove with no remote id is a no-op.
        effective = self._effective_action(action, state)
        if effective is None:
            state.last_status = SyncStatus.REMOVED
            return SyncOutcome(status="skipped", detail="nothing to remove")

        try:
            result = await self._invoke(tenant, listing_id, portal_key, effective, adapter, state)
        except PortalError as exc:
            return self._record_failure(state, exc)

        self._record_success(state, effective, result)
        return SyncOutcome(status=effective.value, detail=result.detail)

    async def _invoke(
        self,
        tenant: TenantContext,
        listing_id: uuid.UUID,
        portal_key: str,
        action: PortalAction,
        adapter: PortalAdapter,
        state: PortalSyncState,
    ) -> PortalResult:
        if action is PortalAction.REMOVE:
            assert state.remote_id is not None  # guarded by _effective_action
            return await adapter.remove(remote_id=state.remote_id)

        payload = await self._payload(tenant, listing_id)
        if payload is None:
            # No longer published — treat a push/update as a withdrawal instead.
            if state.remote_id is None:
                raise PortalError("listing is not published", permanent=True)
            return await adapter.remove(remote_id=state.remote_id)

        if action is PortalAction.UPDATE and state.remote_id is not None:
            return await adapter.update(payload, remote_id=state.remote_id)
        return await adapter.push(payload)

    async def _payload(self, tenant: TenantContext, listing_id: uuid.UUID) -> PortalListing | None:
        payload = await self.listings.portal_payload_for(tenant.id, listing_id)
        if payload is None:
            return None
        photos = await self.media.photo_urls_for(tenant, listing_id)
        # PortalListing is frozen — rebuild with the enriched photo list.
        return replace(payload, photo_urls=photos)

    @staticmethod
    def _effective_action(action: PortalAction, state: PortalSyncState) -> PortalAction | None:
        if action is PortalAction.REMOVE and state.remote_id is None:
            return None  # never pushed — nothing to withdraw
        if action is PortalAction.UPDATE and state.remote_id is None:
            return PortalAction.PUSH  # first sync is always a create
        return action

    def _record_success(
        self, state: PortalSyncState, action: PortalAction, result: PortalResult
    ) -> None:
        now = datetime.now(UTC)
        if action is PortalAction.REMOVE:
            state.last_status = SyncStatus.REMOVED
        else:
            state.last_status = SyncStatus.SYNCED
            if result.remote_id is not None:
                state.remote_id = result.remote_id
        state.last_pushed_at = now
        state.last_error = None
        state.consecutive_failures = 0
        state.circuit_open = False

    def _record_failure(self, state: PortalSyncState, exc: PortalError) -> SyncOutcome:
        state.last_status = SyncStatus.FAILED
        state.last_error = str(exc)
        state.consecutive_failures += 1
        state.retry_count += 1
        if state.consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
            # Breaker trips: pause syncing this pair; surfaced in the admin UI.
            state.circuit_open = True
            state.last_status = SyncStatus.PAUSED
            logger.warning(
                "portal_circuit_opened",
                portal_key=state.portal_key,
                listing_id=str(state.listing_id),
                failures=state.consecutive_failures,
            )
            return SyncOutcome(status="paused", detail=str(exc))
        # A permanent (bad-payload) failure never retries even below the
        # threshold — retrying can't fix it (media-pipeline stance).
        return SyncOutcome(status="failed", retry=not exc.permanent, detail=str(exc))

    async def _state_row(
        self, tenant_id: uuid.UUID, listing_id: uuid.UUID, portal_key: str
    ) -> PortalSyncState:
        state = await self.repo.get(tenant_id, listing_id, portal_key, for_update=True)
        if state is None:
            state = PortalSyncState(
                tenant_id=tenant_id,
                listing_id=listing_id,
                portal_key=portal_key,
                last_status=SyncStatus.PENDING,
            )
            self.repo.add(state)
            await self.repo.flush()
        return state


def _decode_cursor(cursor: str) -> tuple[str, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return values["updated_at"], uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def _enqueue_sync(
    repo: SyndicationRepository,
    tenant_id: uuid.UUID,
    listing_id: uuid.UUID,
    portal_key: str,
    action: PortalAction,
) -> None:
    """Register a post-commit enqueue of the sync task (lazy import — the task
    module imports this service). Primitive args so the task survives a broker
    restart (§12)."""

    async def _enqueue() -> None:
        from app.workers.tasks.syndication import sync_listing_to_portal

        sync_listing_to_portal.delay(str(tenant_id), str(listing_id), portal_key, action.value)

    on_commit(repo.session, _enqueue)


def enqueue_listing_sync(
    session: AsyncSession,
    tenant: TenantContext,
    listing_id: uuid.UUID,
    action: PortalAction,
) -> None:
    """Fan a listing lifecycle event out to every enabled portal, post-commit.

    Called from the listings service's ``transition``/``update`` hooks. No-op
    when the tenant has no portals enabled — the common case pays nothing beyond
    a settings dict lookup. ``session`` is the caller's session (its post-commit
    queue is what fires the enqueue)."""
    portals = enabled_portal_keys(tenant.settings)
    if not portals:
        return
    repo = SyndicationRepository(session)
    for portal_key in portals:
        _enqueue_sync(repo, tenant.id, listing_id, portal_key, action)


def get_syndication_service(session: SessionDep, request: Request) -> SyndicationService:
    settings: Settings = request.app.state.settings
    storage: ObjectStorage = request.app.state.storage
    return SyndicationService(
        SyndicationRepository(session),
        get_listing_service(session),
        build_media_boundary(session, storage, settings),
        build_tenant_boundary(session, request.app.state.redis),
    )


def build_syndication_service_for_worker(
    session: AsyncSession, settings: Settings
) -> SyndicationService:
    """Worker-side construction (no ``request``). The ``sync_to_portal`` path
    reads ``tenant.settings`` directly and never writes tenant config, so the
    tenants boundary's Redis is unused here — a short-lived client from settings
    satisfies the type without a live connection being needed on the hot path."""
    from redis.asyncio import Redis

    return SyndicationService(
        SyndicationRepository(session),
        get_listing_service(session),
        build_media_boundary(session, create_storage(settings), settings),
        build_tenant_boundary(session, Redis.from_url(settings.redis_url, decode_responses=True)),
    )


SyndicationServiceDep = Annotated[SyndicationService, Depends(get_syndication_service)]
