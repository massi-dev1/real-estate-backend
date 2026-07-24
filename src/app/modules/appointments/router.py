"""HTTP layer for appointments & tours (§8.7).

- ``public_router`` — slot search + tour booking on a published agent's
  public profile (rate limited, honeypot-camouflaged like lead capture), and
  the secret-URL iCal feed.
- ``portal_router`` — the agent/manager agenda: list/detail, lifecycle
  transitions, availability editing (keyed by agent profile, ownership via
  the agents service) and iCal URL minting.
"""

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.idempotency import IdempotentRoute
from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import AuthenticatedUser, CurrentUserDep, Permission, require
from app.core.rate_limit import rate_limit
from app.core.tenancy import TenantDep
from app.modules.appointments.models import AppointmentStatus
from app.modules.appointments.schemas import (
    AppointmentOut,
    AppointmentTransitionRequest,
    AvailabilityPut,
    AvailabilityRuleOut,
    IcalUrlOut,
    SlotOut,
    TourBookingCreate,
    TourBookingOut,
)
from app.modules.appointments.service import AppointmentsServiceDep

public_router = APIRouter(tags=["appointments:public"])
# Its own router so the booking POST alone gets Idempotency-Key handling
# (§9) — the pg_advisory_xact_lock in service.book() already kills a true
# double-booking race, but a client retry after a dropped response would
# otherwise still create a second, distinct appointment for the same slot.
booking_idempotent_router = APIRouter(tags=["appointments:public"], route_class=IdempotentRoute)

_booking_limit = rate_limit(key_prefix="tour_booking", limit=5, window_seconds=60)


@public_router.get("/agents/{slug}/slots")
async def get_slots(
    slug: str,
    tenant: TenantDep,
    service: AppointmentsServiceDep,
    date_: date = Query(alias="date"),
) -> list[SlotOut]:
    slots = await service.public_slots(tenant, slug, date_)
    return [SlotOut(start_at=s, end_at=e) for s, e in slots]


@booking_idempotent_router.post(
    "/agents/{slug}/appointments",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_booking_limit)],
)
async def book_tour(
    slug: str,
    data: TourBookingCreate,
    tenant: TenantDep,
    service: AppointmentsServiceDep,
) -> TourBookingOut:
    appointment = await service.book(tenant, slug, data)
    if appointment is None:
        # Honeypot camouflage: a real-shaped response, nothing persisted.
        return TourBookingOut(
            id=uuid.uuid4(),
            status=AppointmentStatus.REQUESTED,
            start_at=data.start_at,
            end_at=data.start_at,
        )
    return TourBookingOut.model_validate(appointment)


@public_router.get("/appointments/ical/{token}")
async def ical_feed(
    token: str,
    tenant: TenantDep,
    service: AppointmentsServiceDep,
) -> Response:
    body = await service.ical_feed(tenant, token)
    return Response(content=body, media_type="text/calendar")


portal_router = APIRouter(prefix="/portal", tags=["appointments:portal"])


@portal_router.get("/appointments")
async def list_appointments(
    tenant: TenantDep,
    service: AppointmentsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.APPOINTMENT_MANAGE)),
    status_filter: AppointmentStatus | None = Query(default=None, alias="status"),
    start_from: datetime | None = Query(default=None),
    start_to: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[AppointmentOut]:
    items, next_cursor, total = await service.list_portal(
        tenant,
        actor,
        status=status_filter,
        start_from=start_from,
        start_to=start_to,
        cursor=cursor,
        limit=limit,
    )
    return Page(
        items=[AppointmentOut.model_validate(x) for x in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )


@portal_router.get("/appointments/{appointment_id}")
async def get_appointment(
    appointment_id: uuid.UUID,
    tenant: TenantDep,
    service: AppointmentsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.APPOINTMENT_MANAGE)),
) -> AppointmentOut:
    return AppointmentOut.model_validate(await service.get_portal(tenant, actor, appointment_id))


@portal_router.post("/appointments/{appointment_id}/status")
async def transition_appointment(
    appointment_id: uuid.UUID,
    data: AppointmentTransitionRequest,
    tenant: TenantDep,
    service: AppointmentsServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.APPOINTMENT_MANAGE)),
) -> AppointmentOut:
    appointment = await service.transition(tenant, actor, appointment_id, data.to_status)
    return AppointmentOut.model_validate(appointment)


# Availability + iCal are keyed by agent *profile* — ownership (own profile
# vs AGENT_MANAGE reach) is enforced by the agents service's scope lookup, so
# no separate permission gate is needed here.


@portal_router.get("/agents/{profile_id}/availability")
async def get_availability(
    profile_id: uuid.UUID,
    tenant: TenantDep,
    service: AppointmentsServiceDep,
    actor: CurrentUserDep,
) -> list[AvailabilityRuleOut]:
    rows = await service.get_availability(tenant, actor, profile_id)
    return [AvailabilityRuleOut.model_validate(r) for r in rows]


@portal_router.put("/agents/{profile_id}/availability")
async def put_availability(
    profile_id: uuid.UUID,
    data: AvailabilityPut,
    tenant: TenantDep,
    service: AppointmentsServiceDep,
    actor: CurrentUserDep,
) -> list[AvailabilityRuleOut]:
    rows = await service.put_availability(tenant, actor, profile_id, data)
    return [AvailabilityRuleOut.model_validate(r) for r in rows]


@portal_router.get("/agents/{profile_id}/ical")
async def get_ical_url(
    profile_id: uuid.UUID,
    tenant: TenantDep,
    service: AppointmentsServiceDep,
    actor: CurrentUserDep,
) -> IcalUrlOut:
    return IcalUrlOut(url=await service.ical_url(tenant, actor, profile_id))
