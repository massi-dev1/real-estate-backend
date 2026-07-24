"""HTTP layer for leads (§8.4).

- ``capture_router`` — the public, unauthenticated lead-capture form. Rate
  limited (§10.8); returns a deliberately minimal shape.
- ``portal_router`` — the back-office: lead/contact CRUD, pipeline
  transitions, activities, the contact timeline, and the tenant's assignment
  policy. RBAC-guarded; ownership scoping happens in the service (§7.2).
"""

import uuid

from fastapi import APIRouter, Depends, Query, status

from app.core.idempotency import IdempotentRoute
from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import AuthenticatedUser, Permission, require
from app.core.rate_limit import rate_limit
from app.core.tenancy import TenantDep
from app.modules.leads.models import Contact, Lead, LeadSource, LeadStage
from app.modules.leads.schemas import (
    ActivityCreate,
    ActivityOut,
    AssignmentRuleOut,
    AssignmentRuleUpdate,
    ContactOut,
    ContactTimelineOut,
    ContactUpdate,
    LeadCaptureCreate,
    LeadCaptureOut,
    LeadCreate,
    LeadDetailOut,
    LeadFilters,
    LeadOut,
    LeadUpdate,
    StageTransitionRequest,
    WhatsAppClickCreate,
    WhatsAppClickOut,
)
from app.modules.leads.service import LeadsServiceDep


def _lead_detail(lead: Lead, contact: Contact) -> LeadDetailOut:
    return LeadDetailOut.model_validate(
        {**LeadOut.model_validate(lead).model_dump(by_alias=False), "contact": contact}
    )


capture_router = APIRouter(prefix="/leads", tags=["leads:public"])
# A dedicated router so /leads/capture alone gets Idempotency-Key handling
# (§9) — a client retrying after a timeout must get back the same lead, not
# a second one. include_router() can't override one route's class, so the
# route sits on its own tiny router instead.
capture_idempotent_router = APIRouter(
    prefix="/leads", tags=["leads:public"], route_class=IdempotentRoute
)

_capture_limit = rate_limit(key_prefix="lead_capture", limit=5, window_seconds=60)


@capture_idempotent_router.post(
    "/capture", status_code=status.HTTP_201_CREATED, dependencies=[Depends(_capture_limit)]
)
async def capture_lead(
    data: LeadCaptureCreate, tenant: TenantDep, service: LeadsServiceDep
) -> LeadCaptureOut:
    lead = await service.capture_lead(tenant, data)
    # Honeypot hits: respond with a real-shaped id but persist nothing — a
    # bot gets no signal that anything was different about this submission.
    return LeadCaptureOut(id=lead.id if lead is not None else uuid.uuid4())


@capture_router.post(
    "/capture/whatsapp-click",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_capture_limit)],
)
async def capture_whatsapp_click(
    data: WhatsAppClickCreate, tenant: TenantDep, service: LeadsServiceDep
) -> WhatsAppClickOut:
    """§8.6 wa.me handoff: the widget POSTs here on click, gets back the
    prefilled deep link, and only then opens WhatsApp — the lead is in the
    CRM before the conversation leaves our system."""
    lead, whatsapp_url = await service.capture_whatsapp_click(tenant, data)
    # Same honeypot camouflage as /capture: a real-shaped id, nothing persisted.
    return WhatsAppClickOut(
        id=lead.id if lead is not None else uuid.uuid4(), whatsapp_url=whatsapp_url
    )


portal_router = APIRouter(prefix="/portal", tags=["leads:portal"])


@portal_router.post("/leads", status_code=status.HTTP_201_CREATED)
async def create_lead(
    data: LeadCreate,
    tenant: TenantDep,
    service: LeadsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LEAD_MANAGE)),
) -> LeadDetailOut:
    lead = await service.create_manual(tenant, actor, data)
    contact = await service.get_contact(tenant, actor, lead.contact_id)
    return _lead_detail(lead, contact)


@portal_router.get("/leads")
async def list_leads(
    tenant: TenantDep,
    service: LeadsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LEAD_MANAGE)),
    stage: LeadStage | None = Query(default=None),
    agent_id: uuid.UUID | None = Query(default=None),
    source: LeadSource | None = Query(default=None),
    listing_id: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[LeadOut]:
    filters = LeadFilters(stage=stage, agent_id=agent_id, source=source, listing_id=listing_id)
    items, next_cursor, total = await service.list_portal(
        tenant, actor, filters=filters, cursor=cursor, limit=limit
    )
    return Page(
        items=[LeadOut.model_validate(x) for x in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )


# Declared before /leads/{lead_id} — path matching is declaration-order, and
# a UUID path param would otherwise swallow "assignment-rule" into a 422.
@portal_router.get("/leads/assignment-rule")
async def get_assignment_rule(
    tenant: TenantDep,
    service: LeadsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LEAD_ASSIGN)),
) -> AssignmentRuleOut:
    return AssignmentRuleOut.model_validate(await service.get_assignment_rule(tenant))


@portal_router.put("/leads/assignment-rule")
async def update_assignment_rule(
    data: AssignmentRuleUpdate,
    tenant: TenantDep,
    service: LeadsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LEAD_ASSIGN)),
) -> AssignmentRuleOut:
    rule = await service.update_assignment_rule(tenant, data.strategy, data.config)
    return AssignmentRuleOut.model_validate(rule)


@portal_router.get("/leads/{lead_id}")
async def get_lead(
    lead_id: uuid.UUID,
    tenant: TenantDep,
    service: LeadsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LEAD_MANAGE)),
) -> LeadDetailOut:
    lead = await service.get_portal(tenant, actor, lead_id)
    contact = await service.get_contact(tenant, actor, lead.contact_id)
    return _lead_detail(lead, contact)


@portal_router.patch("/leads/{lead_id}")
async def update_lead(
    lead_id: uuid.UUID,
    data: LeadUpdate,
    tenant: TenantDep,
    service: LeadsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LEAD_MANAGE)),
) -> LeadOut:
    return LeadOut.model_validate(await service.update(tenant, actor, lead_id, data))


@portal_router.post("/leads/{lead_id}/stage")
async def transition_lead_stage(
    lead_id: uuid.UUID,
    data: StageTransitionRequest,
    tenant: TenantDep,
    service: LeadsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LEAD_MANAGE)),
) -> LeadOut:
    lead = await service.transition_stage(tenant, actor, lead_id, data.to_stage, data.lost_reason)
    return LeadOut.model_validate(lead)


@portal_router.get("/leads/{lead_id}/activities")
async def list_lead_activities(
    lead_id: uuid.UUID,
    tenant: TenantDep,
    service: LeadsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LEAD_MANAGE)),
) -> list[ActivityOut]:
    rows = await service.list_activities(tenant, actor, lead_id)
    return [ActivityOut.model_validate(r) for r in rows]


@portal_router.post("/leads/{lead_id}/activities", status_code=status.HTTP_201_CREATED)
async def create_lead_activity(
    lead_id: uuid.UUID,
    data: ActivityCreate,
    tenant: TenantDep,
    service: LeadsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LEAD_MANAGE)),
) -> ActivityOut:
    activity = await service.record_activity(tenant, actor, lead_id, data)
    return ActivityOut.model_validate(activity)


@portal_router.get("/contacts/{contact_id}")
async def get_contact(
    contact_id: uuid.UUID,
    tenant: TenantDep,
    service: LeadsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LEAD_VIEW_ALL)),
) -> ContactOut:
    return ContactOut.model_validate(await service.get_contact(tenant, actor, contact_id))


# LEAD_VIEW_ALL, not LEAD_MANAGE: contacts aren't agent-owned, so a scoped
# agent must not be able to edit a colleague's contact by UUID (contact ids
# leak via LeadOut.contact_id on shared contacts) — gate the write at least
# as strongly as the read above (review finding).
@portal_router.patch("/contacts/{contact_id}")
async def update_contact(
    contact_id: uuid.UUID,
    data: ContactUpdate,
    tenant: TenantDep,
    service: LeadsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LEAD_VIEW_ALL)),
) -> ContactOut:
    return ContactOut.model_validate(await service.update_contact(tenant, actor, contact_id, data))


@portal_router.get("/contacts/{contact_id}/timeline")
async def get_contact_timeline(
    contact_id: uuid.UUID,
    tenant: TenantDep,
    service: LeadsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LEAD_VIEW_ALL)),
) -> ContactTimelineOut:
    return await service.get_contact_timeline(tenant, actor, contact_id)
