"""DB access for the reviews module. Every method's first arg is ``tenant_id``
(golden rule §5); RLS is the fail-closed safety net beneath it.
"""

import uuid
from datetime import datetime

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.reviews.models import Review, ReviewStatus


class ReviewsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, review: Review) -> None:
        self.session.add(review)

    async def flush(self) -> None:
        await self.session.flush()

    async def get(
        self, tenant_id: uuid.UUID, review_id: uuid.UUID, *, for_update: bool = False
    ) -> Review | None:
        stmt = select(Review).where(Review.tenant_id == tenant_id, Review.id == review_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ---- portal (moderation queue) ----

    def _portal_base(
        self,
        tenant_id: uuid.UUID,
        *,
        status: ReviewStatus | None,
        agent_user_id: uuid.UUID | None,
    ) -> Select[tuple[Review]]:
        stmt = select(Review).where(Review.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(Review.status == status)
        if agent_user_id is not None:
            stmt = stmt.where(Review.agent_user_id == agent_user_id)
        return stmt

    async def list_portal(
        self,
        tenant_id: uuid.UUID,
        *,
        status: ReviewStatus | None,
        agent_user_id: uuid.UUID | None,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Review]:
        """Keyset page on (created_at DESC, id DESC); returns limit+1 rows."""
        stmt = self._portal_base(tenant_id, status=status, agent_user_id=agent_user_id)
        if after is not None:
            stmt = stmt.where(
                or_(
                    Review.created_at < after[0],
                    and_(Review.created_at == after[0], Review.id < after[1]),
                )
            )
        stmt = stmt.order_by(Review.created_at.desc(), Review.id.desc()).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    async def count_portal(
        self,
        tenant_id: uuid.UUID,
        *,
        status: ReviewStatus | None,
        agent_user_id: uuid.UUID | None,
    ) -> int:
        stmt = self._portal_base(
            tenant_id, status=status, agent_user_id=agent_user_id
        ).with_only_columns(func.count())
        return (await self.session.execute(stmt)).scalar_one()

    # ---- public (approved only) ----

    def _public_base(
        self, tenant_id: uuid.UUID, *, agent_user_id: uuid.UUID | None, agency_only: bool
    ) -> Select[tuple[Review]]:
        stmt = select(Review).where(
            Review.tenant_id == tenant_id, Review.status == ReviewStatus.APPROVED
        )
        if agent_user_id is not None:
            stmt = stmt.where(Review.agent_user_id == agent_user_id)
        elif agency_only:
            # Tenant-wide testimonial feed: reviews not tied to any agent.
            stmt = stmt.where(Review.agent_user_id.is_(None))
        return stmt

    async def list_public(
        self,
        tenant_id: uuid.UUID,
        *,
        agent_user_id: uuid.UUID | None,
        agency_only: bool,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Review]:
        stmt = self._public_base(
            tenant_id, agent_user_id=agent_user_id, agency_only=agency_only
        )
        if after is not None:
            stmt = stmt.where(
                or_(
                    Review.created_at < after[0],
                    and_(Review.created_at == after[0], Review.id < after[1]),
                )
            )
        stmt = stmt.order_by(Review.created_at.desc(), Review.id.desc()).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    # ---- aggregates (approved only) ----

    async def aggregate(
        self, tenant_id: uuid.UUID, *, agent_user_id: uuid.UUID | None, agency_only: bool
    ) -> tuple[int, float | None]:
        """(count, average) of approved reviews. ``agent_user_id`` scopes to one
        agent; ``agency_only`` scopes to tenant-wide (no-agent) testimonials;
        both falsy = every approved review in the tenant."""
        stmt = (
            self._public_base(
                tenant_id, agent_user_id=agent_user_id, agency_only=agency_only
            )
            .with_only_columns(func.count(), func.avg(Review.rating))
        )
        count, avg = (await self.session.execute(stmt)).one()
        return int(count), float(avg) if avg is not None else None

    async def aggregate_by_agent(
        self, tenant_id: uuid.UUID, agent_user_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[int, float]]:
        """One GROUP BY for a batch of agents (the public directory) instead of
        N per-agent queries. Agents with no approved review are simply absent."""
        if not agent_user_ids:
            return {}
        stmt = (
            select(
                Review.agent_user_id,
                func.count(),
                func.avg(Review.rating),
            )
            .where(
                Review.tenant_id == tenant_id,
                Review.status == ReviewStatus.APPROVED,
                Review.agent_user_id.in_(agent_user_ids),
            )
            .group_by(Review.agent_user_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return {
            agent_id: (int(count), float(avg))
            for agent_id, count, avg in rows
            if agent_id is not None
        }
