"""DB access for the agents module. Every method's first arg is ``tenant_id``
(golden rule §5). One repository across all three tables — profiles, teams and
membership are always managed together.
"""

import uuid
from datetime import datetime

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agents.models import AgentProfile, Team, TeamMember


class AgentsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- profiles ----

    async def get(
        self, tenant_id: uuid.UUID, profile_id: uuid.UUID, *, for_update: bool = False
    ) -> AgentProfile | None:
        stmt = select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id, AgentProfile.id == profile_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_user(self, tenant_id: uuid.UUID, user_id: uuid.UUID) -> AgentProfile | None:
        stmt = select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id, AgentProfile.user_id == user_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_published_by_slug(
        self, tenant_id: uuid.UUID, slug: str
    ) -> AgentProfile | None:
        stmt = select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.slug == slug,
            AgentProfile.is_published.is_(True),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _published_base(
        self, tenant_id: uuid.UUID, *, specialty: str | None
    ) -> Select[tuple[AgentProfile]]:
        stmt = select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id, AgentProfile.is_published.is_(True)
        )
        if specialty is not None:
            stmt = stmt.where(AgentProfile.specialties.contains([specialty]))
        return stmt

    async def list_published(
        self,
        tenant_id: uuid.UUID,
        *,
        specialty: str | None,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[AgentProfile]:
        """Keyset page on (created_at DESC, id DESC); returns limit+1 rows."""
        stmt = self._published_base(tenant_id, specialty=specialty)
        if after is not None:
            stmt = stmt.where(
                or_(
                    AgentProfile.created_at < after[0],
                    and_(AgentProfile.created_at == after[0], AgentProfile.id < after[1]),
                )
            )
        stmt = stmt.order_by(AgentProfile.created_at.desc(), AgentProfile.id.desc()).limit(
            limit + 1
        )
        return list((await self.session.execute(stmt)).scalars())

    async def count_published(self, tenant_id: uuid.UUID, *, specialty: str | None) -> int:
        stmt = self._published_base(tenant_id, specialty=specialty).with_only_columns(
            func.count()
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def list_portal(self, tenant_id: uuid.UUID) -> list[AgentProfile]:
        """Full roster for the back-office — small per tenant, no pagination."""
        stmt = (
            select(AgentProfile)
            .where(AgentProfile.tenant_id == tenant_id)
            .order_by(AgentProfile.created_at.desc(), AgentProfile.id.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def candidate_user_ids_for_point(
        self, tenant_id: uuid.UUID, lon: float, lat: float
    ) -> list[uuid.UUID]:
        """Published profiles whose service area contains the point — the
        territory-assignment pool (§8.4). GiST-served; deterministic order."""
        point = func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)
        stmt = (
            select(AgentProfile.user_id)
            .where(
                AgentProfile.tenant_id == tenant_id,
                AgentProfile.is_published.is_(True),
                AgentProfile.service_areas.is_not(None),
                func.ST_Contains(AgentProfile.service_areas, point),
            )
            .order_by(AgentProfile.user_id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def any_territory_profile(self, tenant_id: uuid.UUID) -> bool:
        stmt = (
            select(func.count())
            .select_from(AgentProfile)
            .where(
                AgentProfile.tenant_id == tenant_id,
                AgentProfile.is_published.is_(True),
                AgentProfile.service_areas.is_not(None),
            )
        )
        return (await self.session.execute(stmt)).scalar_one() > 0

    async def delete_profile(self, profile: AgentProfile) -> None:
        await self.session.delete(profile)

    # ---- teams ----

    async def get_team(
        self, tenant_id: uuid.UUID, team_id: uuid.UUID, *, for_update: bool = False
    ) -> Team | None:
        stmt = select(Team).where(Team.tenant_id == tenant_id, Team.id == team_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_teams(self, tenant_id: uuid.UUID) -> list[Team]:
        stmt = select(Team).where(Team.tenant_id == tenant_id).order_by(Team.created_at, Team.id)
        return list((await self.session.execute(stmt)).scalars())

    async def delete_team(self, team: Team) -> None:
        await self.session.delete(team)

    # ---- membership ----

    async def list_members(self, tenant_id: uuid.UUID, team_id: uuid.UUID) -> list[TeamMember]:
        stmt = (
            select(TeamMember)
            .where(TeamMember.tenant_id == tenant_id, TeamMember.team_id == team_id)
            .order_by(TeamMember.created_at, TeamMember.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get_member(
        self, tenant_id: uuid.UUID, team_id: uuid.UUID, user_id: uuid.UUID
    ) -> TeamMember | None:
        stmt = select(TeamMember).where(
            TeamMember.tenant_id == tenant_id,
            TeamMember.team_id == team_id,
            TeamMember.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def delete_member(self, member: TeamMember) -> None:
        await self.session.delete(member)

    async def led_team_member_user_ids(
        self, tenant_id: uuid.UUID, lead_user_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """Members of every team the user leads — one query, the team-scoped
        visibility source (§8.5)."""
        stmt = (
            select(TeamMember.user_id)
            .join(Team, Team.id == TeamMember.team_id)
            .where(
                Team.tenant_id == tenant_id,
                TeamMember.tenant_id == tenant_id,
                Team.lead_user_id == lead_user_id,
            )
        )
        return set((await self.session.execute(stmt)).scalars())

    # ---- generic ----

    def add(self, obj: AgentProfile | Team | TeamMember) -> None:
        self.session.add(obj)

    async def flush(self) -> None:
        await self.session.flush()
