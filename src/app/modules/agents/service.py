"""Agents & teams business logic (§8.5).

Ownership model: an agent manages *their own* profile; ``AGENT_MANAGE``
(admins and team leads) manages any. Publishing a profile into the public
directory is manager-only — the directory is curated. Teams are created and
deleted by admins; a team's lead manages its membership.

Boundary accessors for dependent modules (leads' territory strategy, the
team-scoped visibility in listings and leads) live here — those modules never
touch this module's tables.
"""

import uuid
from datetime import datetime
from typing import Annotated

import structlog
from fastapi import Depends, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils.compat import uuid7

from app.common.geo import to_multipolygon
from app.core.config import Settings
from app.core.database import SessionDep, on_commit
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.permissions import AuthenticatedUser, Permission, Role
from app.core.storage import ObjectStorage
from app.core.tenancy import TenantContext
from app.modules.agents.models import AgentProfile, PhotoStatus, Team, TeamMember
from app.modules.agents.repository import AgentsRepository
from app.modules.agents.schemas import (
    AgentProfileCreate,
    AgentProfileUpdate,
    PhotoUploadRequest,
    TeamCreate,
    TeamMemberAdd,
    TeamUpdate,
)
from app.modules.users.service import UserIdentity, UserService, get_user_service
from app.workers.tasks.agents import process_agent_photo
from app.workers.tasks.media import delete_media_objects

logger = structlog.get_logger(__name__)

# Roles that can carry a public agent profile.
PROFILE_ROLES = frozenset({Role.AGENT, Role.TEAM_LEAD})
# AGENT_MANAGE holders whose reach is tenant-wide, not team-scoped (§8.5).
TENANT_WIDE_MANAGER_ROLES = frozenset({Role.ADMIN, Role.MARKETING})


class AgentsService:
    def __init__(
        self,
        repo: AgentsRepository,
        users: UserService,
        storage: ObjectStorage | None = None,
        settings: Settings | None = None,
    ) -> None:
        """``storage``/``settings`` are only needed by the photo pipeline and
        public URL building — the boundary factory used by leads/listings
        (:func:`build_agents_boundary`) omits them."""
        self.repo = repo
        self.users = users
        self._storage = storage
        self._settings = settings

    @property
    def storage(self) -> ObjectStorage:
        assert self._storage is not None, "this AgentsService was built without storage"
        return self._storage

    @property
    def settings(self) -> Settings:
        assert self._settings is not None, "this AgentsService was built without settings"
        return self._settings

    # ---- helpers ----

    @staticmethod
    def _can_manage(actor: AuthenticatedUser) -> bool:
        return actor.has_permission(Permission.AGENT_MANAGE)

    async def _can_manage_profile(
        self, tenant_id: uuid.UUID, actor: AuthenticatedUser, profile: AgentProfile
    ) -> bool:
        """``AGENT_MANAGE`` reaches every profile for admins/marketing, but a
        team lead only reaches profiles of users on a team they lead (§8.5) —
        the permission alone is not an ownership check."""
        if not self._can_manage(actor):
            return False
        if actor.role in TENANT_WIDE_MANAGER_ROLES:
            return True
        if actor.role is Role.TEAM_LEAD:
            return profile.user_id in await self.team_scope_user_ids(tenant_id, actor.id)
        return False

    async def _get_scoped_or_404(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        profile_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> AgentProfile:
        profile = await self.repo.get(tenant.id, profile_id, for_update=for_update)
        if profile is None or not (
            profile.user_id == actor.id
            or await self._can_manage_profile(tenant.id, actor, profile)
        ):
            # 404 for both "doesn't exist" and "not yours" — no existence oracle.
            raise NotFoundError("Agent profile not found.")
        return profile

    async def _flush_or_conflict(self) -> None:
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError(
                "An agent profile with this slug or user already exists."
            ) from exc

    def _photo_objects(self, profile: AgentProfile) -> list[list[str]]:
        objects: list[list[str]] = []
        if profile.photo_key:
            objects.append([self.storage.docs_bucket, profile.photo_key])
        objects.extend(
            [self.storage.media_bucket, variant["key"]]
            for variant in profile.photo_variants.values()
        )
        return objects

    def _enqueue_delete_objects(self, objects: list[list[str]]) -> None:
        if not objects:
            return

        async def _enqueue() -> None:
            delete_media_objects.delay(objects)

        on_commit(self.repo.session, _enqueue)

    # ---- profiles ----

    async def create_profile(
        self, tenant: TenantContext, actor: AuthenticatedUser, data: AgentProfileCreate
    ) -> AgentProfile:
        target_id = data.user_id or actor.id
        if target_id != actor.id and not self._can_manage(actor):
            raise PermissionDeniedError("You can only create your own agent profile.")
        identity = await self.users.get_identity_if_active(tenant.id, target_id)
        if identity is None:
            raise ConflictError("The profile's user does not exist or is not active.")
        if identity.role not in PROFILE_ROLES:
            raise ConflictError("Agent profiles are for agent and team-lead accounts.")
        if await self.repo.get_by_user(tenant.id, target_id) is not None:
            raise ConflictError("This user already has an agent profile.")
        profile = AgentProfile(
            tenant_id=tenant.id,
            user_id=target_id,
            slug=data.slug,
            bio=data.bio or {},
            specialties=data.specialties,
            service_areas=(
                to_multipolygon(data.service_areas) if data.service_areas else None
            ),
            license_no=data.license_no,
            socials=data.socials or {},
        )
        self.repo.add(profile)
        await self._flush_or_conflict()
        return profile

    async def get_portal(
        self, tenant: TenantContext, actor: AuthenticatedUser, profile_id: uuid.UUID
    ) -> AgentProfile:
        return await self._get_scoped_or_404(tenant, actor, profile_id)

    async def get_own(self, tenant: TenantContext, actor: AuthenticatedUser) -> AgentProfile:
        profile = await self.repo.get_by_user(tenant.id, actor.id)
        if profile is None:
            raise NotFoundError("You do not have an agent profile yet.")
        return profile

    async def list_portal(
        self, tenant: TenantContext, actor: AuthenticatedUser
    ) -> list[AgentProfile]:
        if not self._can_manage(actor):
            raise PermissionDeniedError("You do not have permission to view the agent roster.")
        return await self.repo.list_portal(tenant.id)

    async def update_profile(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        profile_id: uuid.UUID,
        data: AgentProfileUpdate,
    ) -> AgentProfile:
        profile = await self._get_scoped_or_404(tenant, actor, profile_id, for_update=True)
        patch = data.model_dump(exclude_unset=True)
        if "is_published" in patch and not self._can_manage(actor):
            # The public directory is curated (mirrors listings' `featured`).
            raise PermissionDeniedError("Only managers can publish an agent profile.")
        if "service_areas" in patch:
            profile.service_areas = (
                to_multipolygon(data.service_areas) if data.service_areas else None
            )
            del patch["service_areas"]
        for field, value in patch.items():
            setattr(profile, field, value)
        await self._flush_or_conflict()
        return profile

    async def delete_profile(
        self, tenant: TenantContext, actor: AuthenticatedUser, profile_id: uuid.UUID
    ) -> None:
        profile = await self._get_scoped_or_404(tenant, actor, profile_id, for_update=True)
        objects = self._photo_objects(profile)
        await self.repo.delete_profile(profile)
        await self.repo.flush()
        # Storage cleanup after commit: a rolled-back delete keeps its objects.
        self._enqueue_delete_objects(objects)

    # ---- photo pipeline (slim §8.2: presign → confirm → media queue) ----

    async def request_photo_upload(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        profile_id: uuid.UUID,
        data: PhotoUploadRequest,
    ) -> tuple[AgentProfile, str, dict[str, str]]:
        profile = await self._get_scoped_or_404(tenant, actor, profile_id, for_update=True)
        if data.size_bytes > self.settings.media_max_upload_bytes:
            limit_mb = self.settings.media_max_upload_bytes // (1024 * 1024)
            raise ConflictError(f"Files larger than {limit_mb} MB are not accepted.")
        # A new upload replaces the previous photo wholesale — old objects are
        # deleted post-commit so a rollback keeps them alive.
        self._enqueue_delete_objects(self._photo_objects(profile))
        profile.photo_key = f"tenants/{tenant.id}/agents/{profile.id}/photo-{uuid7()}"
        profile.photo_status = PhotoStatus.PENDING
        profile.photo_variants = {}
        profile.photo_error = None
        await self.repo.flush()
        upload_url = self.storage.presign_put(
            self.storage.docs_bucket, profile.photo_key, data.content_type
        )
        return profile, upload_url, {"Content-Type": data.content_type}

    async def confirm_photo(
        self, tenant: TenantContext, actor: AuthenticatedUser, profile_id: uuid.UUID
    ) -> AgentProfile:
        profile = await self._get_scoped_or_404(tenant, actor, profile_id, for_update=True)
        if profile.photo_status is not PhotoStatus.PENDING:
            raise ConflictError("There is no pending photo upload to confirm.")
        profile.photo_status = PhotoStatus.PROCESSING
        await self.repo.flush()

        profile_id_str, tenant_id_str = str(profile.id), str(tenant.id)

        async def _enqueue() -> None:
            process_agent_photo.delay(profile_id_str, tenant_id_str)

        on_commit(self.repo.session, _enqueue)
        return profile

    def public_url(self, key: str) -> str:
        return self.storage.public_url(key)

    # ---- public directory ----

    async def list_public(
        self,
        tenant: TenantContext,
        *,
        specialty: str | None,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[tuple[AgentProfile, UserIdentity]], str | None, int]:
        page_size = clamp_limit(limit)
        after = _decode_keyset(cursor) if cursor else None
        rows = await self.repo.list_published(
            tenant.id, specialty=specialty, after=after, limit=page_size
        )
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = encode_cursor(
                {"created_at": last.created_at.isoformat(), "id": str(last.id)}
            )
        identities = await self.users.identities_for(tenant.id, [p.user_id for p in items])
        # A published profile whose account was disabled since simply drops off
        # the directory — no card without a person behind it.
        cards = [(p, identities[p.user_id]) for p in items if p.user_id in identities]
        total = await self.repo.count_published(tenant.id, specialty=specialty)
        return cards, next_cursor, total

    async def get_public(
        self, tenant: TenantContext, slug: str
    ) -> tuple[AgentProfile, UserIdentity]:
        profile = await self.repo.get_published_by_slug(tenant.id, slug)
        if profile is None:
            raise NotFoundError("Agent not found.")
        identity = await self.users.get_identity_if_active(tenant.id, profile.user_id)
        if identity is None:
            raise NotFoundError("Agent not found.")
        return profile, identity

    # ---- boundary accessors (leads, listings) ----

    async def candidates_for_point(
        self, tenant_id: uuid.UUID, lon: float, lat: float
    ) -> list[uuid.UUID]:
        """User ids whose published service area contains the point (§8.4
        territory assignment)."""
        return await self.repo.candidate_user_ids_for_point(tenant_id, lon, lat)

    async def has_territory_data(self, tenant_id: uuid.UUID) -> bool:
        return await self.repo.any_territory_profile(tenant_id)

    async def scope_user_ids_for(
        self, tenant_id: uuid.UUID, actor: AuthenticatedUser
    ) -> set[uuid.UUID] | None:
        """Shared visibility-scoping rule (§8.5): ``None`` means tenant-wide —
        admins and marketing; a team lead sees self + their team members; any
        other role sees only their own. Listings and leads both delegate here
        instead of each re-deriving the ADMIN/MARKETING/TEAM_LEAD/AGENT split."""
        if actor.role in TENANT_WIDE_MANAGER_ROLES:
            return None
        if actor.role is Role.TEAM_LEAD:
            return {actor.id} | await self.team_scope_user_ids(tenant_id, actor.id)
        return {actor.id}

    async def team_scope_user_ids(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """Members of every team the user leads (excluding the user) — the
        §8.5 team-scoped visibility source for listings and leads."""
        members = await self.repo.led_team_member_user_ids(tenant_id, user_id)
        members.discard(user_id)
        return members

    # ---- teams ----

    async def _team_or_404(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        team_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> Team:
        team = await self.repo.get_team(tenant.id, team_id, for_update=for_update)
        if team is None:
            raise NotFoundError("Team not found.")
        if actor.role is not Role.ADMIN and team.lead_user_id != actor.id:
            # AGENT_MANAGE lets a team lead in, but only into *their* team.
            raise NotFoundError("Team not found.")
        return team

    async def create_team(
        self, tenant: TenantContext, actor: AuthenticatedUser, data: TeamCreate
    ) -> Team:
        if actor.role is not Role.ADMIN:
            raise PermissionDeniedError("Only admins can create teams.")
        lead_id = await self._validated_lead(tenant.id, data.lead_user_id)
        team = Team(tenant_id=tenant.id, name=data.name, lead_user_id=lead_id)
        self.repo.add(team)
        await self.repo.flush()
        return team

    async def _validated_lead(
        self, tenant_id: uuid.UUID, lead_user_id: uuid.UUID | None
    ) -> uuid.UUID | None:
        if lead_user_id is None:
            return None
        identity = await self.users.get_identity_if_active(tenant_id, lead_user_id)
        if identity is None or identity.role not in PROFILE_ROLES:
            raise ConflictError("The team lead must be an active agent or team-lead account.")
        return lead_user_id

    async def get_team(
        self, tenant: TenantContext, actor: AuthenticatedUser, team_id: uuid.UUID
    ) -> Team:
        return await self._team_or_404(tenant, actor, team_id)

    async def list_teams(self, tenant: TenantContext, actor: AuthenticatedUser) -> list[Team]:
        teams = await self.repo.list_teams(tenant.id)
        if actor.role is Role.ADMIN:
            return teams
        return [t for t in teams if t.lead_user_id == actor.id]

    async def update_team(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        team_id: uuid.UUID,
        data: TeamUpdate,
    ) -> Team:
        team = await self._team_or_404(tenant, actor, team_id, for_update=True)
        patch = data.model_dump(exclude_unset=True)
        if "lead_user_id" in patch:
            if actor.role is not Role.ADMIN:
                # A lead handing their team to someone else is an admin move.
                raise PermissionDeniedError("Only admins can change a team's lead.")
            team.lead_user_id = await self._validated_lead(tenant.id, data.lead_user_id)
            del patch["lead_user_id"]
        if "name" in patch and data.name is not None:
            team.name = data.name
        await self.repo.flush()
        return team

    async def delete_team(
        self, tenant: TenantContext, actor: AuthenticatedUser, team_id: uuid.UUID
    ) -> None:
        if actor.role is not Role.ADMIN:
            raise PermissionDeniedError("Only admins can delete teams.")
        team = await self._team_or_404(tenant, actor, team_id, for_update=True)
        await self.repo.delete_team(team)
        await self.repo.flush()

    async def list_members(
        self, tenant: TenantContext, actor: AuthenticatedUser, team_id: uuid.UUID
    ) -> list[TeamMember]:
        await self._team_or_404(tenant, actor, team_id)
        return await self.repo.list_members(tenant.id, team_id)

    async def add_member(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        team_id: uuid.UUID,
        data: TeamMemberAdd,
    ) -> TeamMember:
        team = await self._team_or_404(tenant, actor, team_id, for_update=True)
        identity = await self.users.get_identity_if_active(tenant.id, data.user_id)
        if identity is None or identity.role not in PROFILE_ROLES:
            raise ConflictError("Team members must be active agent or team-lead accounts.")
        if await self.repo.get_member(tenant.id, team.id, data.user_id) is not None:
            raise ConflictError("This user is already a member of the team.")
        member = TeamMember(
            tenant_id=tenant.id,
            team_id=team.id,
            user_id=data.user_id,
            role_in_team=data.role_in_team,
        )
        self.repo.add(member)
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError("This user is already a member of the team.") from exc
        return member

    async def remove_member(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        await self._team_or_404(tenant, actor, team_id, for_update=True)
        member = await self.repo.get_member(tenant.id, team_id, user_id)
        if member is None:
            raise NotFoundError("Team member not found.")
        await self.repo.delete_member(member)
        await self.repo.flush()


def _decode_keyset(cursor: str) -> tuple[datetime, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(values["created_at"]), uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def get_agents_service(session: SessionDep, request: Request) -> AgentsService:
    return AgentsService(
        AgentsRepository(session),
        get_user_service(session),
        request.app.state.storage,
        request.app.state.settings,
    )


def build_agents_boundary(session: AsyncSession) -> AgentsService:
    """Storage-free construction for dependent services (leads' territory
    assignment, the team-scoped visibility in listings/leads) that only need
    the boundary accessors."""
    return AgentsService(AgentsRepository(session), get_user_service(session))


AgentsServiceDep = Annotated[AgentsService, Depends(get_agents_service)]
