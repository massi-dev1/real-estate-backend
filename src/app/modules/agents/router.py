"""HTTP layer for agents & teams (§8.5).

- ``public_router`` — the agency site's agent directory and profile pages
  (published profiles only, one negotiated locale, active listings attached).
- ``portal_router`` — profile CRUD + the photo pipeline; ownership checks
  (own profile vs ``AGENT_MANAGE``) live in the service.
- ``teams_router`` — team CRUD and membership; admins manage everything, a
  team's lead manages its membership.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, status

from app.core.i18n import negotiate_locale
from app.core.pagination import Page
from app.core.permissions import AuthenticatedUser, CurrentUserDep, Permission, require
from app.core.tenancy import TenantDep
from app.modules.agents.schemas import (
    AgentProfileCreate,
    AgentProfileOut,
    AgentProfileUpdate,
    AgentStatsOut,
    PhotoUploadOut,
    PhotoUploadRequest,
    PublicAgentDetailOut,
    PublicAgentOut,
    PublicAgentQuery,
    TeamCreate,
    TeamDetailOut,
    TeamMemberAdd,
    TeamMemberOut,
    TeamOut,
    TeamUpdate,
)
from app.modules.agents.service import AgentsServiceDep
from app.modules.leads.service import LeadsServiceDep
from app.modules.listings.schemas import PublicListingOut
from app.modules.listings.service import ListingServiceDep
from app.modules.media.schemas import PublicMediaOut
from app.modules.media.service import MediaServiceDep

# ---- public directory ----

public_router = APIRouter(prefix="/agents", tags=["agents:public"])

# How many active listings a public profile page carries.
PROFILE_LISTINGS_LIMIT = 12


@public_router.get("")
async def list_agents(
    tenant: TenantDep,
    service: AgentsServiceDep,
    query: Annotated[PublicAgentQuery, Query()],
    accept_language: str | None = Header(default=None),
) -> Page[PublicAgentOut]:
    resolved = negotiate_locale(query.locale, accept_language)
    cards, next_cursor, total = await service.list_public(
        tenant, specialty=query.specialty, cursor=query.cursor, limit=query.limit
    )
    return Page(
        items=[
            PublicAgentOut.from_profile(
                profile, identity.display_name, resolved, service.public_url
            )
            for profile, identity in cards
        ],
        next_cursor=next_cursor,
        total_estimate=total,
    )


@public_router.get("/{slug}")
async def get_agent(
    slug: str,
    tenant: TenantDep,
    service: AgentsServiceDep,
    listings: ListingServiceDep,
    media_service: MediaServiceDep,
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> PublicAgentDetailOut:
    resolved = negotiate_locale(locale, accept_language)
    profile, identity = await service.get_public(tenant, slug)
    rows = await listings.public_by_agent(tenant, profile.user_id, limit=PROFILE_LISTINGS_LIMIT)
    covers = await media_service.covers_for(tenant, [x.id for x in rows])
    card = PublicAgentOut.from_profile(
        profile, identity.display_name, resolved, service.public_url
    )
    return PublicAgentDetailOut(
        **card.model_dump(by_alias=False),
        listings=[
            PublicListingOut.from_listing(
                x,
                resolved,
                cover=(
                    PublicMediaOut.from_media(covers[x.id], resolved, media_service.public_url)
                    if x.id in covers
                    else None
                ),
            )
            for x in rows
        ],
    )


# ---- portal: profiles ----

portal_router = APIRouter(prefix="/portal/agents", tags=["agents:portal"])


@portal_router.post("", status_code=status.HTTP_201_CREATED)
async def create_profile(
    data: AgentProfileCreate,
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> AgentProfileOut:
    profile = await service.create_profile(tenant, actor, data)
    return AgentProfileOut.from_profile(profile, service.public_url)


@portal_router.get("")
async def list_profiles(
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.AGENT_MANAGE)),
) -> list[AgentProfileOut]:
    rows = await service.list_portal(tenant, actor)
    return [AgentProfileOut.from_profile(p, service.public_url) for p in rows]


# Declared before /{profile_id} — path matching is declaration-order.
@portal_router.get("/me")
async def get_own_profile(
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> AgentProfileOut:
    profile = await service.get_own(tenant, actor)
    return AgentProfileOut.from_profile(profile, service.public_url)


@portal_router.get("/{profile_id}")
async def get_profile(
    profile_id: uuid.UUID,
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> AgentProfileOut:
    profile = await service.get_portal(tenant, actor, profile_id)
    return AgentProfileOut.from_profile(profile, service.public_url)


@portal_router.patch("/{profile_id}")
async def update_profile(
    profile_id: uuid.UUID,
    data: AgentProfileUpdate,
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> AgentProfileOut:
    profile = await service.update_profile(tenant, actor, profile_id, data)
    return AgentProfileOut.from_profile(profile, service.public_url)


@portal_router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    profile_id: uuid.UUID,
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> None:
    await service.delete_profile(tenant, actor, profile_id)


@portal_router.post("/{profile_id}/photo/uploads", status_code=status.HTTP_201_CREATED)
async def request_photo_upload(
    profile_id: uuid.UUID,
    data: PhotoUploadRequest,
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> PhotoUploadOut:
    profile, upload_url, headers = await service.request_photo_upload(
        tenant, actor, profile_id, data
    )
    return PhotoUploadOut(
        profile=AgentProfileOut.from_profile(profile, service.public_url),
        upload_url=upload_url,
        upload_headers=headers,
        expires_in_seconds=service.settings.media_upload_url_ttl_seconds,
    )


@portal_router.post("/{profile_id}/photo/confirm", status_code=status.HTTP_202_ACCEPTED)
async def confirm_photo(
    profile_id: uuid.UUID,
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> AgentProfileOut:
    profile = await service.confirm_photo(tenant, actor, profile_id)
    return AgentProfileOut.from_profile(profile, service.public_url)


@portal_router.get("/{profile_id}/stats")
async def agent_stats(
    profile_id: uuid.UUID,
    tenant: TenantDep,
    service: AgentsServiceDep,
    listings: ListingServiceDep,
    leads: LeadsServiceDep,
    actor: CurrentUserDep,
) -> AgentStatsOut:
    """§8.5 performance slice: the profile owner sees their own numbers,
    ``AGENT_MANAGE`` sees anyone's (enforced by the profile scope lookup)."""
    profile = await service.get_portal(tenant, actor, profile_id)
    listings_by_status = await listings.counts_by_status_for_agent(tenant.id, profile.user_id)
    leads_by_stage, avg_response = await leads.stats_for_agent(tenant.id, profile.user_id)
    return AgentStatsOut(
        user_id=profile.user_id,
        listings_by_status=listings_by_status,
        leads_by_stage=leads_by_stage,
        avg_first_response_seconds=avg_response,
    )


# ---- portal: teams ----

teams_router = APIRouter(
    prefix="/portal/teams",
    tags=["teams:portal"],
    dependencies=[Depends(require(Permission.AGENT_MANAGE))],
)


@teams_router.post("", status_code=status.HTTP_201_CREATED)
async def create_team(
    data: TeamCreate,
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> TeamOut:
    return TeamOut.model_validate(await service.create_team(tenant, actor, data))


@teams_router.get("")
async def list_teams(
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> list[TeamOut]:
    return [TeamOut.model_validate(t) for t in await service.list_teams(tenant, actor)]


@teams_router.get("/{team_id}")
async def get_team(
    team_id: uuid.UUID,
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> TeamDetailOut:
    team = await service.get_team(tenant, actor, team_id)
    members = await service.list_members(tenant, actor, team_id)
    return TeamDetailOut(
        **TeamOut.model_validate(team).model_dump(by_alias=False),
        members=[TeamMemberOut.model_validate(m) for m in members],
    )


@teams_router.patch("/{team_id}")
async def update_team(
    team_id: uuid.UUID,
    data: TeamUpdate,
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> TeamOut:
    return TeamOut.model_validate(await service.update_team(tenant, actor, team_id, data))


@teams_router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    team_id: uuid.UUID,
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> None:
    await service.delete_team(tenant, actor, team_id)


@teams_router.get("/{team_id}/members")
async def list_team_members(
    team_id: uuid.UUID,
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> list[TeamMemberOut]:
    rows = await service.list_members(tenant, actor, team_id)
    return [TeamMemberOut.model_validate(m) for m in rows]


@teams_router.post("/{team_id}/members", status_code=status.HTTP_201_CREATED)
async def add_team_member(
    team_id: uuid.UUID,
    data: TeamMemberAdd,
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> TeamMemberOut:
    member = await service.add_member(tenant, actor, team_id, data)
    return TeamMemberOut.model_validate(member)


@teams_router.delete("/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_team_member(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    tenant: TenantDep,
    service: AgentsServiceDep,
    actor: CurrentUserDep,
) -> None:
    await service.remove_member(tenant, actor, team_id, user_id)
