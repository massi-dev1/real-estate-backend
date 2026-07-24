"""Appointments & tours business logic (§8.7): availability schedules, public
slot search, tour booking (which mints a CRM lead through the leads service's
capture trunk), the portal lifecycle, reminder dispatch and the iCal feed.

Ownership model mirrors the rest of the portal: an agent manages their own
calendar; ``AGENT_MANAGE`` (via the agents service's profile scoping) manages
another agent's availability, and ``APPOINTMENT_MANAGE`` + the shared
``scope_user_ids_for`` visibility rule bound what appointments an actor sees.

Time model: availability rows are times-of-day interpreted in the tenant's
``settings.appointments.timezone`` (IANA name, default UTC); appointments are
stored as UTC instants.
"""

import uuid
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import structlog
from fastapi import Depends, Request
from icalendar import Calendar, Event

from app.core.config import Settings
from app.core.database import SessionDep, on_commit
from app.core.exceptions import ConflictError, NotFoundError
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.permissions import AuthenticatedUser
from app.core.security import sign_value, unsign_value
from app.core.tenancy import TenantContext
from app.modules.agents.service import AgentsService, build_agents_boundary
from app.modules.appointments.models import (
    AgentAvailability,
    Appointment,
    AppointmentStatus,
)
from app.modules.appointments.repository import AppointmentsRepository
from app.modules.appointments.schemas import (
    MAX_BOOKING_DAYS_AHEAD,
    AvailabilityPut,
    TourBookingCreate,
)
from app.modules.leads.service import LeadsService, get_leads_service
from app.modules.listings.service import ListingService, get_listing_service
from app.workers.tasks.email import send_email

logger = structlog.get_logger(__name__)

DEFAULT_SLOT_MINUTES = 60
DEFAULT_BUFFER_MINUTES = 0
# iCal feeds carry a bounded look-back so a recently-finished tour doesn't
# vanish from the agent's calendar app mid-day.
ICAL_LOOKBACK = timedelta(days=1)
ICAL_MAX_EVENTS = 500
_ICAL_PURPOSE = "agent-ical"

# §8.7 lifecycle: requested → confirmed → completed | no_show, cancellable
# from either live state. No path back into `requested`.
_TRANSITIONS: dict[AppointmentStatus, frozenset[AppointmentStatus]] = {
    AppointmentStatus.REQUESTED: frozenset(
        {AppointmentStatus.CONFIRMED, AppointmentStatus.CANCELLED}
    ),
    AppointmentStatus.CONFIRMED: frozenset(
        {AppointmentStatus.COMPLETED, AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW}
    ),
    AppointmentStatus.COMPLETED: frozenset(),
    AppointmentStatus.CANCELLED: frozenset(),
    AppointmentStatus.NO_SHOW: frozenset(),
}


def _as_utc(value: datetime) -> datetime:
    """Normalize to an aware UTC instant (client payloads may be naive)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _tenant_appointment_settings(tenant: TenantContext) -> tuple[int, int, tzinfo]:
    raw: dict[str, Any] = tenant.settings.get("appointments") or {}
    slot = raw.get("slot_minutes")
    buffer = raw.get("buffer_minutes")
    slot_minutes = slot if isinstance(slot, int) and 5 <= slot <= 480 else DEFAULT_SLOT_MINUTES
    buffer_minutes = (
        buffer if isinstance(buffer, int) and 0 <= buffer <= 240 else DEFAULT_BUFFER_MINUTES
    )
    # Free-form JSONB: an unknown zone name degrades to UTC instead of taking
    # the public slot search down. Plain UTC avoids the tzdata lookup.
    tz: tzinfo = UTC
    zone = raw.get("timezone")
    if isinstance(zone, str) and zone and zone.upper() != "UTC":
        try:
            tz = ZoneInfo(zone)
        except Exception:
            logger.warning("appointments_bad_timezone", zone=zone, tenant_id=str(tenant.id))
    return slot_minutes, buffer_minutes, tz


class AppointmentsService:
    def __init__(
        self,
        repo: AppointmentsRepository,
        agents: AgentsService,
        leads: LeadsService,
        listings: ListingService,
        settings: Settings,
    ) -> None:
        self.repo = repo
        self.agents = agents
        self.leads = leads
        self.listings = listings
        self.settings = settings

    # ---- availability (portal, keyed by agent profile) ----

    async def get_availability(
        self, tenant: TenantContext, actor: AuthenticatedUser, profile_id: uuid.UUID
    ) -> list[AgentAvailability]:
        # get_portal inherits the own-or-manager scoping (404 otherwise).
        profile = await self.agents.get_portal(tenant, actor, profile_id)
        return await self.repo.list_availability(tenant.id, profile.user_id)

    async def put_availability(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        profile_id: uuid.UUID,
        data: AvailabilityPut,
    ) -> list[AgentAvailability]:
        """Full replacement — the schedule is one document, not row-by-row
        CRUD, so the client can never end up half-updated."""
        profile = await self.agents.get_portal(tenant, actor, profile_id)
        await self.repo.delete_availability(tenant.id, profile.user_id)
        for rule in data.rules:
            self.repo.add(
                AgentAvailability(
                    tenant_id=tenant.id,
                    agent_user_id=profile.user_id,
                    day_of_week=rule.day_of_week,
                    date=rule.date,
                    start_time=rule.start_time,
                    end_time=rule.end_time,
                    is_block=rule.is_block,
                )
            )
        await self.repo.flush()
        return await self.repo.list_availability(tenant.id, profile.user_id)

    # ---- slot computation (public) ----

    async def public_slots(
        self, tenant: TenantContext, slug: str, day: date
    ) -> list[tuple[datetime, datetime]]:
        profile, _ = await self.agents.get_public(tenant, slug)
        self._validate_day(tenant, day)
        return await self._free_slots(tenant, profile.user_id, day)

    def _validate_day(self, tenant: TenantContext, day: date) -> None:
        _, _, tz = _tenant_appointment_settings(tenant)
        today = datetime.now(tz).date()
        if day < today or day > today + timedelta(days=MAX_BOOKING_DAYS_AHEAD):
            raise ConflictError(f"Tours can be booked up to {MAX_BOOKING_DAYS_AHEAD} days ahead.")

    async def _free_slots(
        self, tenant: TenantContext, agent_user_id: uuid.UUID, day: date
    ) -> list[tuple[datetime, datetime]]:
        """Availability minus blocks minus (busy appointments ± buffer), cut
        into a fixed grid stepping from each window's start."""
        slot_minutes, buffer_minutes, tz = _tenant_appointment_settings(tenant)
        slot_len = timedelta(minutes=slot_minutes)
        buffer = timedelta(minutes=buffer_minutes)

        rows = await self.repo.availability_for_date(tenant.id, agent_user_id, day)
        exceptions = [r for r in rows if r.date is not None]
        open_windows = [
            self._window_utc(day, r.start_time, r.end_time, tz)
            for r in rows
            if r.day_of_week is not None
        ] + [
            self._window_utc(day, r.start_time, r.end_time, tz)
            for r in exceptions
            if not r.is_block
        ]
        blocks = [
            self._window_utc(day, r.start_time, r.end_time, tz) for r in exceptions if r.is_block
        ]
        if not open_windows:
            return []

        day_start = min(w[0] for w in open_windows)
        day_end = max(w[1] for w in open_windows)
        busy = [
            (_as_utc(a.start_at) - buffer, _as_utc(a.end_at) + buffer)
            for a in await self.repo.active_between(
                tenant.id, agent_user_id, day_start - buffer, day_end + buffer
            )
        ]

        now = datetime.now(UTC)
        slots: list[tuple[datetime, datetime]] = []
        for window_start, window_end in sorted(open_windows):
            cursor = window_start
            while cursor + slot_len <= window_end:
                slot = (cursor, cursor + slot_len)
                if (
                    slot[0] > now
                    and not _overlaps_any(slot, blocks)
                    and not _overlaps_any(slot, busy)
                    and slot not in slots  # overlapping windows can duplicate
                ):
                    slots.append(slot)
                cursor += slot_len
        slots.sort()
        return slots

    @staticmethod
    def _window_utc(day: date, start: time, end: time, tz: tzinfo) -> tuple[datetime, datetime]:
        return (
            datetime.combine(day, start, tzinfo=tz).astimezone(UTC),
            datetime.combine(day, end, tzinfo=tz).astimezone(UTC),
        )

    # ---- booking (public) ----

    async def book(
        self, tenant: TenantContext, slug: str, data: TourBookingCreate
    ) -> Appointment | None:
        """Honeypot hits return ``None`` — the router fabricates a real-shaped
        response (mirrors lead capture, checked before any lookup so a bot
        can't fingerprint the path via a distinguishable 404/409)."""
        if data.hp:
            logger.info("tour_booking_honeypot_triggered")
            return None

        profile, _ = await self.agents.get_public(tenant, slug)
        _, _, tz = _tenant_appointment_settings(tenant)
        start_at = _as_utc(data.start_at)
        day = start_at.astimezone(tz).date()
        self._validate_day(tenant, day)

        if data.listing_id is not None:
            # Inherits the published-only 404, same as lead capture.
            await self.listings.get_public(tenant, str(data.listing_id))

        # Serialize competing bookings for this agent, then re-derive the free
        # slots inside the lock — the request's start must match one exactly.
        await self.repo.lock_agent_calendar(tenant.id, profile.user_id)
        slots = await self._free_slots(tenant, profile.user_id, day)
        slot = next((s for s in slots if s[0] == start_at), None)
        if slot is None:
            raise ConflictError("This time slot is not available. Please pick another.")

        appointment = Appointment(
            tenant_id=tenant.id,
            agent_user_id=profile.user_id,
            listing_id=data.listing_id,
            start_at=slot[0],
            end_at=slot[1],
        )

        lead = await self.leads.register_tour_request(
            tenant,
            data.contact,
            listing_id=data.listing_id,
            message=data.message,
            agent_user_id=profile.user_id,
            source_meta=_booking_source_meta(data),
        )
        appointment.contact_id = lead.contact_id
        appointment.lead_id = lead.id
        self.repo.add(appointment)
        await self.repo.flush()
        await self.leads.log_tour_activity(
            tenant.id,
            lead.id,
            {
                "event": "tour_requested",
                "appointment_id": str(appointment.id),
                "start_at": slot[0].isoformat(),
            },
        )
        return appointment

    # ---- portal lifecycle ----

    async def _scoped_or_404(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        appointment_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> Appointment:
        appointment = await self.repo.get(
            tenant.id,
            appointment_id,
            scope_user_ids=await self.agents.scope_user_ids_for(tenant.id, actor),
            for_update=for_update,
        )
        if appointment is None:
            # 404 for both "doesn't exist" and "not yours" — no existence oracle.
            raise NotFoundError("Appointment not found.")
        return appointment

    async def get_portal(
        self, tenant: TenantContext, actor: AuthenticatedUser, appointment_id: uuid.UUID
    ) -> Appointment:
        return await self._scoped_or_404(tenant, actor, appointment_id)

    async def list_portal(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        *,
        status: AppointmentStatus | None,
        start_from: datetime | None,
        start_to: datetime | None,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[Appointment], str | None, int]:
        page_size = clamp_limit(limit)
        after = _decode_keyset(cursor) if cursor else None
        scope = await self.agents.scope_user_ids_for(tenant.id, actor)
        rows = await self.repo.list_page(
            tenant.id,
            scope_user_ids=scope,
            status=status,
            start_from=_as_utc(start_from) if start_from else None,
            start_to=_as_utc(start_to) if start_to else None,
            after=after,
            limit=page_size,
        )
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = encode_cursor(
                {"start_at": _as_utc(last.start_at).isoformat(), "id": str(last.id)}
            )
        total = await self.repo.count(
            tenant.id,
            scope_user_ids=scope,
            status=status,
            start_from=_as_utc(start_from) if start_from else None,
            start_to=_as_utc(start_to) if start_to else None,
        )
        return items, next_cursor, total

    async def transition(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        appointment_id: uuid.UUID,
        to_status: AppointmentStatus,
    ) -> Appointment:
        appointment = await self._scoped_or_404(tenant, actor, appointment_id, for_update=True)
        if to_status not in _TRANSITIONS[appointment.status]:
            raise ConflictError(
                f"An appointment cannot move from '{appointment.status.value}' "
                f"to '{to_status.value}'."
            )
        appointment.status = to_status
        if to_status is AppointmentStatus.CONFIRMED:
            appointment.confirmed_at = datetime.now(UTC)

        if appointment.lead_id is not None:
            payload = {
                "event": f"tour_{to_status.value}",
                "appointment_id": str(appointment.id),
                "start_at": _as_utc(appointment.start_at).isoformat(),
            }
            if to_status is AppointmentStatus.NO_SHOW:
                # Timeline entry + fixed score penalty, via the leads boundary.
                await self.leads.record_no_show(tenant.id, appointment.lead_id, payload)
            else:
                await self.leads.log_tour_activity(tenant.id, appointment.lead_id, payload)

        await self.repo.flush()
        if to_status in (AppointmentStatus.CONFIRMED, AppointmentStatus.CANCELLED):
            await self._notify_contact(tenant, appointment, to_status)
        return appointment

    async def _notify_contact(
        self, tenant: TenantContext, appointment: Appointment, to_status: AppointmentStatus
    ) -> None:
        contacts = await self.leads.contacts_by_ids(tenant.id, [appointment.contact_id])
        contact = contacts.get(appointment.contact_id)
        if contact is None or not contact.email:
            return
        when = _as_utc(appointment.start_at).strftime("%Y-%m-%d %H:%M UTC")
        if to_status is AppointmentStatus.CONFIRMED:
            subject = "Your visit is confirmed"
            body = f"Your property visit on {when} has been confirmed. See you there!"
        else:
            subject = "Your visit was cancelled"
            body = f"Your property visit on {when} has been cancelled. Please book a new slot."
        email = contact.email

        async def _send() -> None:
            send_email.delay(to=email, subject=subject, text=body)

        # Post-commit: a rolled-back transition must not email anyone.
        on_commit(self.repo.session, _send)

    # ---- iCal feed (§8.7) ----

    async def ical_url(
        self, tenant: TenantContext, actor: AuthenticatedUser, profile_id: uuid.UUID
    ) -> str:
        """Secret-URL token: stateless HMAC (outlives any Redis TTL, §10.12),
        purpose-separated, pinned to tenant + agent."""
        profile = await self.agents.get_portal(tenant, actor, profile_id)
        token = sign_value(_ICAL_PURPOSE, f"{tenant.id}:{profile.user_id}", self.settings)
        return f"/api/v1/appointments/ical/{token}"

    async def ical_feed(self, tenant: TenantContext, token: str) -> bytes:
        value = unsign_value(_ICAL_PURPOSE, token, self.settings)
        if value is None:
            raise NotFoundError("Calendar not found.")
        tenant_raw, _, user_raw = value.partition(":")
        try:
            token_tenant, agent_user_id = uuid.UUID(tenant_raw), uuid.UUID(user_raw)
        except ValueError as exc:
            raise NotFoundError("Calendar not found.") from exc
        if token_tenant != tenant.id:
            # A token minted on one agency's domain is useless on another's.
            raise NotFoundError("Calendar not found.")

        appointments = await self.repo.upcoming_for_agent(
            tenant.id,
            agent_user_id,
            since=datetime.now(UTC) - ICAL_LOOKBACK,
            limit=ICAL_MAX_EVENTS,
        )
        contacts = await self.leads.contacts_by_ids(tenant.id, [a.contact_id for a in appointments])

        # icalendar ships py.typed but leaves Component.__init__ untyped —
        # the constructors need scoped ignores under strict mypy.
        calendar = Calendar()  # type: ignore[no-untyped-call]
        calendar.add("prodid", "-//Real Estate Platform//Tours//EN")
        calendar.add("version", "2.0")
        for appointment in appointments:
            contact = contacts.get(appointment.contact_id)
            name = ""
            if contact is not None:
                name = " ".join(p for p in (contact.first_name, contact.last_name) if p)
            confirmed = appointment.status is AppointmentStatus.CONFIRMED
            event = Event()  # type: ignore[no-untyped-call]
            event.add("uid", f"{appointment.id}@{tenant.slug}")
            event.add("dtstart", _as_utc(appointment.start_at))
            event.add("dtend", _as_utc(appointment.end_at))
            event.add("summary", f"Property tour — {name}" if name else "Property tour")
            event.add("status", "CONFIRMED" if confirmed else "TENTATIVE")
            calendar.add_component(event)
        return bytes(calendar.to_ical())


def _overlaps_any(
    slot: tuple[datetime, datetime], windows: list[tuple[datetime, datetime]]
) -> bool:
    return any(slot[0] < end and slot[1] > start for start, end in windows)


def _booking_source_meta(data: TourBookingCreate) -> dict[str, Any]:
    return {
        k: v
        for k, v in {
            "utm_source": data.utm_source,
            "utm_medium": data.utm_medium,
            "utm_campaign": data.utm_campaign,
            "page": data.page,
            "referrer": data.referrer,
        }.items()
        if v is not None
    }


def _decode_keyset(cursor: str) -> tuple[datetime, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(values["start_at"]), uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def get_appointments_service(session: SessionDep, request: Request) -> AppointmentsService:
    return AppointmentsService(
        AppointmentsRepository(session),
        build_agents_boundary(session),
        get_leads_service(session),
        get_listing_service(session),
        request.app.state.settings,
    )


AppointmentsServiceDep = Annotated[AppointmentsService, Depends(get_appointments_service)]
