"""Reviews business logic (§8.11).

Flow: a public visitor submits a review (rating + text about an agent, or the
agency itself) → it lands ``PENDING`` → a moderator (``REVIEW_MODERATE``)
approves or rejects it → only approved rows feed the public feed and the
per-agent / per-tenant aggregates.

Cross-module boundaries: the agent target is resolved through
``AgentsService.published_user_id_for_slug`` and the optional listing context
through ``ListingService.get_public`` — both storage-free boundary services
(reviews never touches those modules' tables). The reverse direction (agents
folding review aggregates into its profile/stats) is composed at the router
layer, not by an agents→reviews service dependency.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionDep
from app.core.exceptions import ConflictError, NotFoundError
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.permissions import AuthenticatedUser
from app.core.tenancy import TenantContext
from app.modules.agents.service import AgentsService, build_agents_boundary
from app.modules.listings.service import ListingService, get_listing_service
from app.modules.reviews.models import Review, ReviewStatus
from app.modules.reviews.repository import ReviewsRepository
from app.modules.reviews.schemas import ReviewModerateIn, ReviewSubmitIn

logger = structlog.get_logger(__name__)


class ReviewsService:
    def __init__(
        self,
        repo: ReviewsRepository,
        agents: AgentsService,
        listings: ListingService,
    ) -> None:
        self.repo = repo
        self.agents = agents
        self.listings = listings

    # ---- public submission ----

    async def submit(self, tenant: TenantContext, data: ReviewSubmitIn) -> Review | None:
        """Persist a PENDING review, or ``None`` on a honeypot hit (the router
        fabricates a real-shaped ack so a bot sees no difference — same
        camouflage as lead capture). The agent target and the listing context
        are both resolved against *published* data — a review can't be attached
        to a hidden agent or an unpublished listing (and an unknown slug/ref is
        a 404-shaped user error, not a silent orphan)."""
        if data.hp:
            # Honeypot filled: short-circuit before any DB work, persist nothing.
            return None

        agent_user_id: uuid.UUID | None = None
        if data.agent_slug is not None:
            agent_user_id = await self.agents.published_user_id_for_slug(tenant.id, data.agent_slug)
            if agent_user_id is None:
                raise NotFoundError("Agent not found.")

        listing_id: uuid.UUID | None = None
        if data.listing_ref is not None:
            # get_public raises NotFoundError for an unknown/unpublished ref —
            # let it surface, mirroring how the other capture surfaces reject a
            # bogus listing reference rather than swallowing it.
            listing = await self.listings.get_public(tenant, data.listing_ref)
            listing_id = listing.id

        review = Review(
            tenant_id=tenant.id,
            agent_user_id=agent_user_id,
            listing_id=listing_id,
            rating=data.rating,
            title=data.title,
            body=data.body,
            author_name=data.author_name,
            author_email=data.author_email,
            status=ReviewStatus.PENDING,
        )
        self.repo.add(review)
        await self.repo.flush()
        return review

    # ---- portal (moderation) ----

    async def get(self, tenant: TenantContext, review_id: uuid.UUID) -> Review:
        review = await self.repo.get(tenant.id, review_id)
        if review is None:
            raise NotFoundError("Review not found.")
        return review

    async def list_portal(
        self,
        tenant: TenantContext,
        *,
        status: ReviewStatus | None,
        agent_user_id: uuid.UUID | None,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[Review], str | None, int]:
        page_size = clamp_limit(limit)
        after = _decode_keyset(cursor) if cursor else None
        rows = await self.repo.list_portal(
            tenant.id,
            status=status,
            agent_user_id=agent_user_id,
            after=after,
            limit=page_size,
        )
        items = rows[:page_size]
        next_cursor = _next_cursor(rows, items, page_size)
        total = await self.repo.count_portal(tenant.id, status=status, agent_user_id=agent_user_id)
        return items, next_cursor, total

    async def moderate(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        review_id: uuid.UUID,
        data: ReviewModerateIn,
    ) -> Review:
        """Approve or reject a pending review. Idempotent to the same terminal
        state (re-approving an approved review just refreshes the stamp), but a
        review can't flip approved↔rejected once decided — that would silently
        re-expose or hide a testimonial, so it's a 409."""
        review = await self.repo.get(tenant.id, review_id, for_update=True)
        if review is None:
            raise NotFoundError("Review not found.")
        if review.status is not ReviewStatus.PENDING and review.status != data.status:
            raise ConflictError(
                "This review has already been moderated. Delete it to change the decision."
            )
        review.status = data.status
        if data.is_verified is not None:
            review.is_verified = data.is_verified
        review.moderated_by = actor.id
        review.moderated_at = datetime.now(UTC)
        review.moderation_note = data.note
        await self.repo.flush()
        return review

    async def delete(self, tenant: TenantContext, review_id: uuid.UUID) -> None:
        review = await self.repo.get(tenant.id, review_id, for_update=True)
        if review is None:
            raise NotFoundError("Review not found.")
        await self.repo.session.delete(review)
        await self.repo.flush()

    # ---- public feed ----

    async def list_public(
        self,
        tenant: TenantContext,
        *,
        agent_user_id: uuid.UUID | None,
        agency_only: bool,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[Review], str | None]:
        page_size = clamp_limit(limit)
        after = _decode_keyset(cursor) if cursor else None
        rows = await self.repo.list_public(
            tenant.id,
            agent_user_id=agent_user_id,
            agency_only=agency_only,
            after=after,
            limit=page_size,
        )
        items = rows[:page_size]
        return items, _next_cursor(rows, items, page_size)

    async def list_public_for_slug(
        self,
        tenant: TenantContext,
        slug: str,
        *,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[Review], str | None]:
        user_id = await self.agents.published_user_id_for_slug(tenant.id, slug)
        if user_id is None:
            raise NotFoundError("Agent not found.")
        return await self.list_public(
            tenant, agent_user_id=user_id, agency_only=False, cursor=cursor, limit=limit
        )

    # ---- aggregates (boundary accessors for agents' profile/stats) ----

    async def aggregate_for_agent(
        self, tenant_id: uuid.UUID, agent_user_id: uuid.UUID
    ) -> tuple[int, float | None]:
        return await self.repo.aggregate(tenant_id, agent_user_id=agent_user_id, agency_only=False)

    async def aggregate_for_tenant(self, tenant_id: uuid.UUID) -> tuple[int, float | None]:
        """Every approved review in the tenant — the agency-wide rating."""
        return await self.repo.aggregate(tenant_id, agent_user_id=None, agency_only=False)

    async def aggregates_by_agent(
        self, tenant_id: uuid.UUID, agent_user_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[int, float]]:
        """Batch rollup for the public agent directory — one GROUP BY."""
        return await self.repo.aggregate_by_agent(tenant_id, agent_user_ids)


def _next_cursor(rows: list[Review], items: list[Review], page_size: int) -> str | None:
    if len(rows) <= page_size:
        return None
    last = items[-1]
    return encode_cursor({"created_at": last.created_at.isoformat(), "id": str(last.id)})


def _decode_keyset(cursor: str) -> tuple[datetime, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(values["created_at"]), uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def get_reviews_service(session: SessionDep, request: Request) -> ReviewsService:
    return ReviewsService(
        ReviewsRepository(session),
        build_agents_boundary(session),
        get_listing_service(session),
    )


def build_reviews_boundary(session: AsyncSession) -> ReviewsService:
    """For dependent composition (the agents router folds review aggregates
    into a profile/stats response) — same construction, name kept parallel to
    the other ``build_*_boundary`` factories."""
    return ReviewsService(
        ReviewsRepository(session),
        build_agents_boundary(session),
        get_listing_service(session),
    )


ReviewsServiceDep = Annotated[ReviewsService, Depends(get_reviews_service)]
