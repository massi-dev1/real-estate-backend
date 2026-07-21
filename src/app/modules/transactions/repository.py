"""DB access for deals, milestones & documents. Every method's first arg is
``tenant_id`` (golden rule §5)."""

import uuid
from datetime import date, datetime

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.transactions.models import (
    CLOSED_STATUSES,
    Deal,
    DealDocument,
    DealMilestone,
    DealStatus,
)


class TransactionsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- deals ----

    async def get_deal(
        self,
        tenant_id: uuid.UUID,
        deal_id: uuid.UUID,
        *,
        scope_user_ids: set[uuid.UUID] | None = None,
        for_update: bool = False,
    ) -> Deal | None:
        stmt = select(Deal).where(Deal.tenant_id == tenant_id, Deal.id == deal_id)
        if scope_user_ids is not None:
            stmt = stmt.where(Deal.owner_user_id.in_(scope_user_ids))
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _list_base(
        self,
        tenant_id: uuid.UUID,
        *,
        scope_user_ids: set[uuid.UUID] | None,
        status: DealStatus | None,
    ) -> Select[tuple[Deal]]:
        stmt = select(Deal).where(Deal.tenant_id == tenant_id)
        if scope_user_ids is not None:
            stmt = stmt.where(Deal.owner_user_id.in_(scope_user_ids))
        if status is not None:
            stmt = stmt.where(Deal.status == status)
        return stmt

    async def list_deals(
        self,
        tenant_id: uuid.UUID,
        *,
        scope_user_ids: set[uuid.UUID] | None,
        status: DealStatus | None,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Deal]:
        """Keyset page on (created_at DESC, id DESC) — newest deals first;
        returns limit+1 rows."""
        stmt = self._list_base(tenant_id, scope_user_ids=scope_user_ids, status=status)
        if after is not None:
            stmt = stmt.where(
                or_(
                    Deal.created_at < after[0],
                    and_(Deal.created_at == after[0], Deal.id < after[1]),
                )
            )
        stmt = stmt.order_by(Deal.created_at.desc(), Deal.id.desc()).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    async def count_deals(
        self,
        tenant_id: uuid.UUID,
        *,
        scope_user_ids: set[uuid.UUID] | None,
        status: DealStatus | None,
    ) -> int:
        stmt = self._list_base(
            tenant_id, scope_user_ids=scope_user_ids, status=status
        ).with_only_columns(func.count())
        return (await self.session.execute(stmt)).scalar_one()

    def add_deal(self, deal: Deal) -> None:
        self.session.add(deal)

    async def delete_deal(self, deal: Deal) -> None:
        await self.session.delete(deal)

    # ---- milestones ----

    async def get_milestone(
        self,
        tenant_id: uuid.UUID,
        deal_id: uuid.UUID,
        milestone_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> DealMilestone | None:
        stmt = select(DealMilestone).where(
            DealMilestone.tenant_id == tenant_id,
            DealMilestone.deal_id == deal_id,
            DealMilestone.id == milestone_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_milestones(
        self, tenant_id: uuid.UUID, deal_id: uuid.UUID
    ) -> list[DealMilestone]:
        stmt = (
            select(DealMilestone)
            .where(
                DealMilestone.tenant_id == tenant_id,
                DealMilestone.deal_id == deal_id,
            )
            .order_by(DealMilestone.position, DealMilestone.created_at, DealMilestone.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    def add_milestone(self, milestone: DealMilestone) -> None:
        self.session.add(milestone)

    async def delete_milestone(self, milestone: DealMilestone) -> None:
        await self.session.delete(milestone)

    async def due_milestones(
        self, tenant_id: uuid.UUID, *, on_or_before: date, limit: int
    ) -> list[tuple[DealMilestone, Deal]]:
        """Uncompleted, unreminded milestones with a due_date on or before the
        cutoff, on a non-closed deal — the reminder sweep's work set. Joins the
        deal so the sweep can resolve the owner without a second query."""
        stmt = (
            select(DealMilestone, Deal)
            .join(Deal, DealMilestone.deal_id == Deal.id)
            .where(
                DealMilestone.tenant_id == tenant_id,
                DealMilestone.completed_at.is_(None),
                DealMilestone.reminder_sent_at.is_(None),
                DealMilestone.due_date.is_not(None),
                DealMilestone.due_date <= on_or_before,
                Deal.status.not_in(CLOSED_STATUSES),
            )
            .order_by(DealMilestone.due_date, DealMilestone.id)
            .limit(limit)
        )
        return [(m, d) for m, d in (await self.session.execute(stmt)).all()]

    # ---- documents ----

    async def get_document(
        self,
        tenant_id: uuid.UUID,
        deal_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> DealDocument | None:
        stmt = select(DealDocument).where(
            DealDocument.tenant_id == tenant_id,
            DealDocument.deal_id == deal_id,
            DealDocument.id == document_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_documents(self, tenant_id: uuid.UUID, deal_id: uuid.UUID) -> list[DealDocument]:
        stmt = (
            select(DealDocument)
            .where(
                DealDocument.tenant_id == tenant_id,
                DealDocument.deal_id == deal_id,
            )
            .order_by(DealDocument.created_at, DealDocument.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    def add_document(self, document: DealDocument) -> None:
        self.session.add(document)

    async def delete_document(self, document: DealDocument) -> None:
        await self.session.delete(document)

    # ---- generic ----

    async def flush(self) -> None:
        await self.session.flush()
