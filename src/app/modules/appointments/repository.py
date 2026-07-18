"""DB access for appointments & availability. Every method's first arg is
``tenant_id`` (golden rule §5)."""

import uuid
from datetime import date, datetime

from sqlalchemy import Select, and_, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.appointments.models import (
    AgentAvailability,
    Appointment,
    AppointmentStatus,
)

# Statuses that occupy a slot (and get reminders): everything still "live".
ACTIVE_STATUSES = (AppointmentStatus.REQUESTED, AppointmentStatus.CONFIRMED)


class AppointmentsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- availability ----

    async def list_availability(
        self, tenant_id: uuid.UUID, agent_user_id: uuid.UUID
    ) -> list[AgentAvailability]:
        stmt = (
            select(AgentAvailability)
            .where(
                AgentAvailability.tenant_id == tenant_id,
                AgentAvailability.agent_user_id == agent_user_id,
            )
            .order_by(
                AgentAvailability.day_of_week.nulls_last(),
                AgentAvailability.date,
                AgentAvailability.start_time,
            )
        )
        return list((await self.session.execute(stmt)).scalars())

    async def delete_availability(self, tenant_id: uuid.UUID, agent_user_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(AgentAvailability).where(
                AgentAvailability.tenant_id == tenant_id,
                AgentAvailability.agent_user_id == agent_user_id,
            )
        )

    async def availability_for_date(
        self, tenant_id: uuid.UUID, agent_user_id: uuid.UUID, day: date
    ) -> list[AgentAvailability]:
        """Weekly rows for the weekday plus dated exceptions for the day."""
        stmt = select(AgentAvailability).where(
            AgentAvailability.tenant_id == tenant_id,
            AgentAvailability.agent_user_id == agent_user_id,
            or_(
                AgentAvailability.day_of_week == day.weekday(),
                AgentAvailability.date == day,
            ),
        )
        return list((await self.session.execute(stmt)).scalars())

    # ---- appointments ----

    async def get(
        self,
        tenant_id: uuid.UUID,
        appointment_id: uuid.UUID,
        *,
        scope_user_ids: set[uuid.UUID] | None = None,
        for_update: bool = False,
    ) -> Appointment | None:
        stmt = select(Appointment).where(
            Appointment.tenant_id == tenant_id, Appointment.id == appointment_id
        )
        if scope_user_ids is not None:
            stmt = stmt.where(Appointment.agent_user_id.in_(scope_user_ids))
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _list_base(
        self,
        tenant_id: uuid.UUID,
        *,
        scope_user_ids: set[uuid.UUID] | None,
        status: AppointmentStatus | None,
        start_from: datetime | None,
        start_to: datetime | None,
    ) -> Select[tuple[Appointment]]:
        stmt = select(Appointment).where(Appointment.tenant_id == tenant_id)
        if scope_user_ids is not None:
            stmt = stmt.where(Appointment.agent_user_id.in_(scope_user_ids))
        if status is not None:
            stmt = stmt.where(Appointment.status == status)
        if start_from is not None:
            stmt = stmt.where(Appointment.start_at >= start_from)
        if start_to is not None:
            stmt = stmt.where(Appointment.start_at < start_to)
        return stmt

    async def list_page(
        self,
        tenant_id: uuid.UUID,
        *,
        scope_user_ids: set[uuid.UUID] | None,
        status: AppointmentStatus | None,
        start_from: datetime | None,
        start_to: datetime | None,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Appointment]:
        """Keyset page on (start_at ASC, id ASC) — an agenda reads forward in
        time; returns limit+1 rows."""
        stmt = self._list_base(
            tenant_id,
            scope_user_ids=scope_user_ids,
            status=status,
            start_from=start_from,
            start_to=start_to,
        )
        if after is not None:
            stmt = stmt.where(
                or_(
                    Appointment.start_at > after[0],
                    and_(Appointment.start_at == after[0], Appointment.id > after[1]),
                )
            )
        stmt = stmt.order_by(Appointment.start_at, Appointment.id).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    async def count(
        self,
        tenant_id: uuid.UUID,
        *,
        scope_user_ids: set[uuid.UUID] | None,
        status: AppointmentStatus | None,
        start_from: datetime | None,
        start_to: datetime | None,
    ) -> int:
        stmt = self._list_base(
            tenant_id,
            scope_user_ids=scope_user_ids,
            status=status,
            start_from=start_from,
            start_to=start_to,
        ).with_only_columns(func.count())
        return (await self.session.execute(stmt)).scalar_one()

    async def active_between(
        self,
        tenant_id: uuid.UUID,
        agent_user_id: uuid.UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> list[Appointment]:
        """Live (requested/confirmed) appointments overlapping the window —
        the busy set the slot computation subtracts."""
        stmt = select(Appointment).where(
            Appointment.tenant_id == tenant_id,
            Appointment.agent_user_id == agent_user_id,
            Appointment.status.in_(ACTIVE_STATUSES),
            Appointment.start_at < window_end,
            Appointment.end_at > window_start,
        )
        return list((await self.session.execute(stmt)).scalars())

    async def upcoming_for_agent(
        self, tenant_id: uuid.UUID, agent_user_id: uuid.UUID, *, since: datetime, limit: int
    ) -> list[Appointment]:
        """The iCal feed's window: live appointments from ``since`` forward."""
        stmt = (
            select(Appointment)
            .where(
                Appointment.tenant_id == tenant_id,
                Appointment.agent_user_id == agent_user_id,
                Appointment.status.in_(ACTIVE_STATUSES),
                Appointment.start_at >= since,
            )
            .order_by(Appointment.start_at, Appointment.id)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def lock_agent_calendar(self, tenant_id: uuid.UUID, agent_user_id: uuid.UUID) -> None:
        """Transaction-scoped advisory lock serializing bookings per agent —
        there is no row to ``FOR UPDATE`` before the appointment exists, and
        without a mutex two concurrent bookings can both see the slot free."""
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"appointments:{tenant_id}:{agent_user_id}"},
        )

    # ---- generic ----

    def add(self, obj: Appointment | AgentAvailability) -> None:
        self.session.add(obj)

    async def flush(self) -> None:
        await self.session.flush()
