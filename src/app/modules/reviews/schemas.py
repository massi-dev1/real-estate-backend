"""Reviews schemas (§8.11).

Public submission carries the same §10.8 spam defense as every other public
capture surface (honeypot ``hp`` + ``rendered_at`` min-fill / max-age), reusing
the leads constants so the whole app agrees on what "too fast" / "stale" means.
The public output is deliberately minimal (no author email, no moderation
metadata) — an embeddable testimonial, nothing more.
"""

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from app.core.schema import InputSchema, OutSchema
from app.modules.leads.schemas import MAX_FORM_AGE_SECONDS, MIN_FILL_SECONDS
from app.modules.reviews.models import ReviewStatus


class ReviewSubmitIn(InputSchema):
    """Anonymous public review submission — always lands PENDING for moderation.

    The target is an agent (``agent_slug``, resolved to the published profile's
    user id server-side) or, when omitted, the agency itself (a tenant-wide
    testimonial). ``listing_ref`` is optional context.
    """

    agent_slug: str | None = Field(default=None, max_length=120)
    listing_ref: str | None = Field(default=None, max_length=120)
    rating: int = Field(ge=1, le=5)
    title: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    author_name: str = Field(min_length=1, max_length=120)
    author_email: str | None = Field(default=None, max_length=320)
    # Spam defense (§10.8): a hidden field real browsers never fill, and the
    # render timestamp — an instant (headless) submit fails the min-fill check.
    # The router drops a honeypot hit silently (fake-shaped 201), same stance
    # as lead capture.
    hp: str = ""
    rendered_at: datetime

    @field_validator("author_email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        return value.strip().lower() or None if value else None

    @field_validator("body", "author_name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def not_too_fast(self) -> Self:
        # hp being set is handled by the router (silent drop) — don't 422 on it
        # here, that would tell a bot the honeypot exists.
        if self.hp:
            return self
        now = datetime.now(self.rendered_at.tzinfo) if self.rendered_at.tzinfo else datetime.now()
        elapsed = (now - self.rendered_at).total_seconds()
        if elapsed < MIN_FILL_SECONDS:
            raise ValueError("form submitted too quickly")
        if elapsed > MAX_FORM_AGE_SECONDS:
            raise ValueError("form is stale — please reload and resubmit")
        return self


class ReviewSubmitOut(OutSchema):
    """Minimal ack — an unauthenticated caller learns nothing beyond "received".
    A review is never visible until a moderator approves it."""

    id: uuid.UUID
    status: ReviewStatus


class ReviewModerateIn(InputSchema):
    """Moderation decision. Only ``approved``/``rejected`` are reachable — a
    review can't be moved back to ``pending`` (the queue is one-way)."""

    status: ReviewStatus
    is_verified: bool | None = None
    note: str | None = Field(default=None, max_length=500)

    @field_validator("status")
    @classmethod
    def terminal_only(cls, value: ReviewStatus) -> ReviewStatus:
        if value is ReviewStatus.PENDING:
            raise ValueError("a review can only be approved or rejected")
        return value


class ReviewOut(OutSchema):
    """Portal / moderation view — the full row."""

    id: uuid.UUID
    agent_user_id: uuid.UUID | None
    listing_id: uuid.UUID | None
    rating: int
    title: str | None
    body: str
    author_name: str
    author_email: str | None
    status: ReviewStatus
    is_verified: bool
    moderated_by: uuid.UUID | None
    moderated_at: datetime | None
    moderation_note: str | None
    created_at: datetime
    updated_at: datetime


class PublicReviewOut(OutSchema):
    """Embeddable testimonial — no email, no moderation metadata."""

    id: uuid.UUID
    agent_user_id: uuid.UUID | None
    rating: int
    title: str | None
    body: str
    author_name: str
    is_verified: bool
    created_at: datetime

    @classmethod
    def from_review(cls, review: Any) -> "PublicReviewOut":
        return cls(
            id=review.id,
            agent_user_id=review.agent_user_id,
            rating=review.rating,
            title=review.title,
            body=review.body,
            author_name=review.author_name,
            is_verified=review.is_verified,
            created_at=review.created_at,
        )


class ReviewAggregateOut(OutSchema):
    """Rating rollup — the number a profile/home page shows next to the stars.
    ``average`` is ``None`` when there are no approved reviews yet."""

    count: int
    average: float | None
