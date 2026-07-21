"""HTTP layer for reviews (§8.11).

- ``public_router`` — anonymous submission (rate-limited, honeypot-camouflaged
  like every other public capture surface) plus the embeddable approved-review
  feed: per-agent (``/agents/{slug}/reviews``) and agency-wide (``/reviews``),
  each with its rating aggregate.
- ``portal_router`` — the moderation queue: list/filter, approve/reject, delete.
  Gated by ``REVIEW_MODERATE``.
"""

import uuid

from fastapi import APIRouter, Depends, Query, status

from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import AuthenticatedUser, Permission, require
from app.core.rate_limit import rate_limit
from app.core.tenancy import TenantDep
from app.modules.reviews.models import ReviewStatus
from app.modules.reviews.schemas import (
    PublicReviewOut,
    ReviewAggregateOut,
    ReviewModerateIn,
    ReviewOut,
    ReviewSubmitIn,
    ReviewSubmitOut,
)
from app.modules.reviews.service import ReviewsServiceDep

public_router = APIRouter(tags=["reviews:public"])

_submit_limit = rate_limit(key_prefix="review_submit", limit=5, window_seconds=60)


@public_router.post(
    "/reviews",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_submit_limit)],
)
async def submit_review(
    data: ReviewSubmitIn, tenant: TenantDep, service: ReviewsServiceDep
) -> ReviewSubmitOut:
    """Anonymous submission — always lands PENDING. A honeypot hit gets a
    real-shaped ack with a fabricated id and nothing is persisted."""
    review = await service.submit(tenant, data)
    if review is None:
        return ReviewSubmitOut(id=uuid.uuid4(), status=ReviewStatus.PENDING)
    return ReviewSubmitOut(id=review.id, status=review.status)


@public_router.get("/reviews")
async def list_agency_reviews(
    tenant: TenantDep,
    service: ReviewsServiceDep,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[PublicReviewOut]:
    """Agency-wide testimonials (reviews not tied to any specific agent)."""
    items, next_cursor = await service.list_public(
        tenant, agent_user_id=None, agency_only=True, cursor=cursor, limit=limit
    )
    return Page(
        items=[PublicReviewOut.from_review(r) for r in items],
        next_cursor=next_cursor,
        total_estimate=None,
    )


@public_router.get("/reviews/summary")
async def agency_review_summary(
    tenant: TenantDep, service: ReviewsServiceDep
) -> ReviewAggregateOut:
    """The tenant-wide rating across every approved review (§8.5 aggregation)."""
    count, average = await service.aggregate_for_tenant(tenant.id)
    return ReviewAggregateOut(count=count, average=average)


@public_router.get("/agents/{slug}/reviews")
async def list_agent_reviews(
    slug: str,
    tenant: TenantDep,
    service: ReviewsServiceDep,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[PublicReviewOut]:
    items, next_cursor = await service.list_public_for_slug(
        tenant, slug, cursor=cursor, limit=limit
    )
    return Page(
        items=[PublicReviewOut.from_review(r) for r in items],
        next_cursor=next_cursor,
        total_estimate=None,
    )


portal_router = APIRouter(prefix="/portal/reviews", tags=["reviews:portal"])


@portal_router.get("")
async def list_reviews(
    tenant: TenantDep,
    service: ReviewsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.REVIEW_MODERATE)),
    status_filter: ReviewStatus | None = Query(default=None, alias="status"),
    agent_user_id: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[ReviewOut]:
    items, next_cursor, total = await service.list_portal(
        tenant,
        status=status_filter,
        agent_user_id=agent_user_id,
        cursor=cursor,
        limit=limit,
    )
    return Page(
        items=[ReviewOut.model_validate(r) for r in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )


@portal_router.get("/{review_id}")
async def get_review(
    review_id: uuid.UUID,
    tenant: TenantDep,
    service: ReviewsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.REVIEW_MODERATE)),
) -> ReviewOut:
    return ReviewOut.model_validate(await service.get(tenant, review_id))


@portal_router.post("/{review_id}/moderate")
async def moderate_review(
    review_id: uuid.UUID,
    data: ReviewModerateIn,
    tenant: TenantDep,
    service: ReviewsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.REVIEW_MODERATE)),
) -> ReviewOut:
    return ReviewOut.model_validate(await service.moderate(tenant, actor, review_id, data))


@portal_router.delete("/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review(
    review_id: uuid.UUID,
    tenant: TenantDep,
    service: ReviewsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.REVIEW_MODERATE)),
) -> None:
    await service.delete(tenant, review_id)
