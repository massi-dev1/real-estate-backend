"""DB access for the leads module. Every method's first arg is ``tenant_id``
(golden rule §5). One repository across all five tables — they are always
queried together (a lead is meaningless without its contact/activities), so
splitting into five classes would be ceremony without an isolation benefit.
"""

import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.leads.models import (
    AssignmentRule,
    AssignmentStrategy,
    Contact,
    DripStopReason,
    Lead,
    LeadActivity,
    LeadDripState,
    LeadStage,
)
from app.modules.leads.schemas import LeadFilters


class LeadsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- contacts ----

    async def find_contact_for_dedupe(
        self, tenant_id: uuid.UUID, *, email: str | None, phone: str | None
    ) -> Contact | None:
        """Email match takes priority over phone (service-level policy);
        callers try email first, then phone, as two separate calls."""
        if email is not None:
            stmt = select(Contact).where(
                Contact.tenant_id == tenant_id, func.lower(Contact.email) == email.lower()
            )
            found = (await self.session.execute(stmt)).scalar_one_or_none()
            if found is not None:
                return found
        if phone is not None:
            stmt = select(Contact).where(Contact.tenant_id == tenant_id, Contact.phone == phone)
            return (await self.session.execute(stmt)).scalar_one_or_none()
        return None

    async def get_contact(self, tenant_id: uuid.UUID, contact_id: uuid.UUID) -> Contact | None:
        stmt = select(Contact).where(Contact.tenant_id == tenant_id, Contact.id == contact_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def contacts_by_ids(
        self, tenant_id: uuid.UUID, contact_ids: Collection[uuid.UUID]
    ) -> list[Contact]:
        if not contact_ids:
            return []
        stmt = select(Contact).where(
            Contact.tenant_id == tenant_id, Contact.id.in_(list(contact_ids))
        )
        return list((await self.session.execute(stmt)).scalars())

    # ---- leads ----

    def _lead_base(
        self, tenant_id: uuid.UUID, *, scope_user_ids: Collection[uuid.UUID] | None = None
    ) -> Select[tuple[Lead]]:
        stmt = select(Lead).where(Lead.tenant_id == tenant_id)
        if scope_user_ids is not None:
            stmt = stmt.where(Lead.agent_id.in_(list(scope_user_ids)))
        return stmt

    async def get_lead(
        self,
        tenant_id: uuid.UUID,
        lead_id: uuid.UUID,
        *,
        scope_user_ids: Collection[uuid.UUID] | None = None,
        for_update: bool = False,
    ) -> Lead | None:
        stmt = self._lead_base(tenant_id, scope_user_ids=scope_user_ids).where(
            Lead.id == lead_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _apply_filters(
        self, stmt: Select[tuple[Lead]], filters: LeadFilters
    ) -> Select[tuple[Lead]]:
        if filters.stage is not None:
            stmt = stmt.where(Lead.stage == filters.stage)
        if filters.agent_id is not None:
            stmt = stmt.where(Lead.agent_id == filters.agent_id)
        if filters.source is not None:
            stmt = stmt.where(Lead.source == filters.source)
        if filters.listing_id is not None:
            stmt = stmt.where(Lead.listing_id == filters.listing_id)
        return stmt

    async def list_leads(
        self,
        tenant_id: uuid.UUID,
        *,
        scope_user_ids: Collection[uuid.UUID] | None,
        filters: LeadFilters,
        contact_id: uuid.UUID | None = None,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Lead]:
        """Keyset page on (created_at DESC, id DESC); returns limit+1 rows."""
        stmt = self._apply_filters(
            self._lead_base(tenant_id, scope_user_ids=scope_user_ids), filters
        )
        if contact_id is not None:
            stmt = stmt.where(Lead.contact_id == contact_id)
        if after is not None:
            stmt = stmt.where(
                or_(
                    Lead.created_at < after[0],
                    and_(Lead.created_at == after[0], Lead.id < after[1]),
                )
            )
        stmt = stmt.order_by(Lead.created_at.desc(), Lead.id.desc()).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    async def count_leads(
        self,
        tenant_id: uuid.UUID,
        *,
        scope_user_ids: Collection[uuid.UUID] | None,
        filters: LeadFilters,
    ) -> int:
        stmt = self._apply_filters(
            self._lead_base(tenant_id, scope_user_ids=scope_user_ids), filters
        ).with_only_columns(func.count())
        return (await self.session.execute(stmt)).scalar_one()

    async def stats_for_agent(
        self, tenant_id: uuid.UUID, agent_user_id: uuid.UUID
    ) -> tuple[dict[str, int], float | None]:
        """(leads-by-stage counts, avg first-response seconds) for one agent —
        the §8.5 performance slice, two aggregate queries."""
        by_stage_stmt = (
            select(Lead.stage, func.count())
            .where(Lead.tenant_id == tenant_id, Lead.agent_id == agent_user_id)
            .group_by(Lead.stage)
        )
        rows = (await self.session.execute(by_stage_stmt)).all()
        by_stage = {stage.value: count for stage, count in rows}

        avg_stmt = select(
            func.avg(
                func.extract("epoch", Lead.first_response_at)
                - func.extract("epoch", Lead.created_at)
            )
        ).where(
            Lead.tenant_id == tenant_id,
            Lead.agent_id == agent_user_id,
            Lead.first_response_at.is_not(None),
        )
        avg_seconds = (await self.session.execute(avg_stmt)).scalar_one()
        return by_stage, float(avg_seconds) if avg_seconds is not None else None

    async def funnel_counts_for_day(
        self, tenant_id: uuid.UUID, day_start: datetime, day_end: datetime
    ) -> tuple[int, int, int]:
        """Cohort funnel for leads *created* on the given day: how many were
        created, and of those how many are now won / lost. Keyed on
        ``created_at`` so a nightly re-run of the same day recomputes identical
        numbers (the analytics rollup upserts, never accumulates). ``day_end`` is
        exclusive."""
        won = LeadStage.WON
        lost = LeadStage.LOST
        stmt = select(
            func.count(),
            func.count().filter(Lead.stage == won),
            func.count().filter(Lead.stage == lost),
        ).where(
            Lead.tenant_id == tenant_id,
            Lead.created_at >= day_start,
            Lead.created_at < day_end,
        )
        created, won_n, lost_n = (await self.session.execute(stmt)).one()
        return int(created), int(won_n), int(lost_n)

    async def source_counts_for_day(
        self, tenant_id: uuid.UUID, day_start: datetime, day_end: datetime
    ) -> list[tuple[str, int, int]]:
        """Per-source cohort counts (created, now-won) for leads created on the
        day — the source-performance rollup. ``day_end`` is exclusive."""
        stmt = (
            select(
                Lead.source,
                func.count(),
                func.count().filter(Lead.stage == LeadStage.WON),
            )
            .where(
                Lead.tenant_id == tenant_id,
                Lead.created_at >= day_start,
                Lead.created_at < day_end,
            )
            .group_by(Lead.source)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(source.value, int(created), int(won)) for source, created, won in rows]

    async def open_lead_counts(
        self, tenant_id: uuid.UUID, agent_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Open (not won/lost) lead counts per agent in one aggregate query —
        the round-robin picker runs on every public capture, so one query per
        candidate would put an N+1 on an unauthenticated hot path (review
        finding)."""
        if not agent_ids:
            return {}
        stmt = (
            select(Lead.agent_id, func.count())
            .where(
                Lead.tenant_id == tenant_id,
                Lead.agent_id.in_(agent_ids),
                Lead.stage.not_in((LeadStage.WON, LeadStage.LOST)),
            )
            .group_by(Lead.agent_id)
        )
        rows = (await self.session.execute(stmt)).all()
        return dict(rows)  # type: ignore[arg-type]

    # ---- activities ----

    async def list_activities_for_lead(
        self, tenant_id: uuid.UUID, lead_id: uuid.UUID
    ) -> list[LeadActivity]:
        stmt = (
            select(LeadActivity)
            .where(LeadActivity.tenant_id == tenant_id, LeadActivity.lead_id == lead_id)
            .order_by(LeadActivity.created_at.desc(), LeadActivity.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def list_activities_for_leads(
        self, tenant_id: uuid.UUID, lead_ids: list[uuid.UUID]
    ) -> list[LeadActivity]:
        if not lead_ids:
            return []
        stmt = (
            select(LeadActivity)
            .where(LeadActivity.tenant_id == tenant_id, LeadActivity.lead_id.in_(lead_ids))
            .order_by(LeadActivity.created_at.desc(), LeadActivity.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    # ---- assignment rules ----

    async def get_assignment_rule(self, tenant_id: uuid.UUID) -> AssignmentRule | None:
        stmt = select(AssignmentRule).where(AssignmentRule.tenant_id == tenant_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert_assignment_rule(
        self, tenant_id: uuid.UUID, *, strategy: AssignmentStrategy, config: dict[str, Any]
    ) -> AssignmentRule:
        """Get-or-create on the ``(tenant_id)`` unique constraint. One row per
        tenant, written rarely (manager config changes) — a race would just
        mean the loser's write is superseded, not a lost/duplicated resource
        like the reference-counter's high-frequency case, so a plain
        select-then-write (versus a raw-SQL atomic upsert) is proportionate."""
        rule = await self.get_assignment_rule(tenant_id)
        if rule is None:
            rule = AssignmentRule(tenant_id=tenant_id, strategy=strategy, config=config)
            self.session.add(rule)
        else:
            rule.strategy = strategy
            rule.config = config
        await self.session.flush()
        return rule

    # ---- drip state ----

    async def get_drip_state(
        self, tenant_id: uuid.UUID, lead_id: uuid.UUID
    ) -> LeadDripState | None:
        stmt = select(LeadDripState).where(
            LeadDripState.tenant_id == tenant_id, LeadDripState.lead_id == lead_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_due_drips(
        self, tenant_id: uuid.UUID, *, now: datetime, limit: int
    ) -> list[LeadDripState]:
        stmt = (
            select(LeadDripState)
            .where(
                LeadDripState.tenant_id == tenant_id,
                LeadDripState.stopped_at.is_(None),
                LeadDripState.next_send_at <= now,
            )
            .order_by(LeadDripState.next_send_at)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def stop_drip(
        self, tenant_id: uuid.UUID, lead_id: uuid.UUID, reason: DripStopReason
    ) -> None:
        drip = await self.get_drip_state(tenant_id, lead_id)
        if drip is None or drip.stopped_at is not None:
            return
        drip.stopped_at = datetime.now(UTC)
        drip.stopped_reason = reason

    # ---- generic ----

    def add(self, obj: Contact | Lead | LeadActivity | AssignmentRule | LeadDripState) -> None:
        self.session.add(obj)

    async def flush(self) -> None:
        await self.session.flush()

    async def refresh(
        self,
        obj: Contact | Lead | LeadActivity | AssignmentRule | LeadDripState,
        fields: list[str] | None = None,
    ) -> None:
        await self.session.refresh(obj, fields)
