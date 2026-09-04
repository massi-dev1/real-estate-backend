"""Appointments & tours (§8.7): availability schedules, public slot search,
tour booking (which mints a CRM lead), the portal lifecycle with contact
emails, the no-show score penalty, the Beat reminder sweep and the secret-URL
iCal feed."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient
from icalendar import Calendar
from sqlalchemy import update

from app.core.database import set_tenant_guc
from app.core.permissions import Role
from app.modules.appointments.models import Appointment
from app.workers.tasks.appointments import send_tour_reminders
from tests.helpers import HOST_A, HOST_B, bearer, mailpit_code, register_user
from tests.test_agents import make_profile, publish_profile
from tests.test_leads import PORTAL_LEADS, mailpit_count
from tests.test_listings import add_user, make_listing, tenant_and_login, transition
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

PORTAL_APPOINTMENTS = "/api/v1/portal/appointments"
PORTAL_AGENTS = "/api/v1/portal/agents"
SLUG = "sam-the-agent"  # PROFILE_BODY's default


def weekly_rules(start: str = "09:00:00", end: str = "12:00:00") -> list[dict[str, Any]]:
    return [{"dayOfWeek": d, "startTime": start, "endTime": end} for d in range(7)]


def tomorrow() -> date:
    return (datetime.now(UTC) + timedelta(days=1)).date()


def slot_at(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=UTC)


def booking_body(start_at: datetime, *, email: str, **overrides: Any) -> dict[str, Any]:
    return {
        "contact": {"firstName": "Visitor", "email": email},
        "startAt": start_at.isoformat(),
        "renderedAt": (datetime.now(UTC) - timedelta(seconds=30)).isoformat(),
        **overrides,
    }


async def setup_published_agent(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    *,
    rules: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, str], dict[str, str], str, dict[str, Any]]:
    """Tenant + admin + a published agent profile with a weekly schedule."""
    tenant, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    agent = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="tours@a.example.com"
    )
    agent_id: str = (await client.get("/api/v1/users/me", headers=agent)).json()["id"]
    profile = await make_profile(client, agent)
    await publish_profile(client, admin, profile["id"])
    if rules is not None:
        await put_availability(client, agent, profile["id"], rules)
    return tenant, admin, agent, agent_id, profile


async def put_availability(
    client: AsyncClient, headers: dict[str, str], profile_id: str, rules: list[dict[str, Any]]
) -> Any:
    return await client.put(
        f"{PORTAL_AGENTS}/{profile_id}/availability", json={"rules": rules}, headers=headers
    )


async def get_slots(client: AsyncClient, day: date) -> list[dict[str, Any]]:
    resp = await client.get(
        f"/api/v1/agents/{SLUG}/slots", params={"date": day.isoformat()}, headers={"Host": HOST_A}
    )
    assert resp.status_code == 200, resp.text
    return list(resp.json())


async def book(client: AsyncClient, start_at: datetime, *, email: str, **overrides: Any) -> Any:
    return await client.post(
        f"/api/v1/agents/{SLUG}/appointments",
        json=booking_body(start_at, email=email, **overrides),
        headers={"Host": HOST_A},
    )


async def transition_appointment(
    client: AsyncClient, headers: dict[str, str], appointment_id: str, to_status: str
) -> Any:
    return await client.post(
        f"{PORTAL_APPOINTMENTS}/{appointment_id}/status",
        json={"toStatus": to_status},
        headers=headers,
    )


async def assert_transition(
    client: AsyncClient,
    headers: dict[str, str],
    appointment_id: str,
    to_status: str,
    expected: int,
) -> Any:
    resp = await transition_appointment(client, headers, appointment_id, to_status)
    assert resp.status_code == expected, resp.text
    return resp


async def shift_appointment(
    app: FastAPI, tenant_id: str, appointment_id: str, *, starts_in: timedelta
) -> None:
    """Backdate/advance an appointment so reminder windows can be exercised."""
    start = datetime.now(UTC) + starts_in
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, uuid.UUID(tenant_id))
        await session.execute(
            update(Appointment)
            .where(Appointment.id == uuid.UUID(appointment_id))
            .values(start_at=start, end_at=start + timedelta(hours=1))
        )


# ---- availability ----


async def test_availability_put_get_and_scoping(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin, agent, _, profile = await setup_published_agent(
        client, platform_headers, create_tenant_user
    )
    day = tomorrow()
    rules = [
        *weekly_rules(),
        {"date": day.isoformat(), "startTime": "10:00:00", "endTime": "11:00:00", "isBlock": True},
    ]
    resp = await put_availability(client, agent, profile["id"], rules)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 8
    assert next(r for r in body if r["isBlock"])["date"] == day.isoformat()

    # A rule must be weekly XOR dated.
    bad = await put_availability(
        client,
        agent,
        profile["id"],
        [{"dayOfWeek": 1, "date": day.isoformat(), "startTime": "09:00:00", "endTime": "10:00:00"}],
    )
    assert bad.status_code == 422

    # A foreign agent gets a 404 (no oracle); an admin manages any schedule.
    other = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="other@a.example.com"
    )
    assert (await put_availability(client, other, profile["id"], weekly_rules())).status_code == 404
    admin_put = await put_availability(client, admin, profile["id"], rules)
    assert admin_put.status_code == 200

    got = await client.get(f"{PORTAL_AGENTS}/{profile['id']}/availability", headers=agent)
    assert got.status_code == 200
    assert len(got.json()) == 8


# ---- public slots & booking ----


async def test_slots_reflect_availability_blocks_and_bookings(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, _, agent, _, profile = await setup_published_agent(
        client, platform_headers, create_tenant_user, rules=weekly_rules()
    )
    day = tomorrow()
    slots = await get_slots(client, day)
    assert [s["startAt"] for s in slots] == [
        slot_at(day, h).isoformat().replace("+00:00", "Z") for h in (9, 10, 11)
    ]

    # A dated block carves out its window from the weekly template.
    await put_availability(
        client,
        agent,
        profile["id"],
        [
            *weekly_rules(),
            {
                "date": day.isoformat(),
                "startTime": "10:00:00",
                "endTime": "11:00:00",
                "isBlock": True,
            },
        ],
    )
    assert len(await get_slots(client, day)) == 2

    # A booking removes its slot from the public search.
    resp = await book(client, slot_at(day, 9), email="v1@example.com")
    assert resp.status_code == 201, resp.text
    remaining = await get_slots(client, day)
    assert [s["startAt"] for s in remaining] == [
        slot_at(day, 11).isoformat().replace("+00:00", "Z")
    ]


async def test_booking_creates_appointment_and_lead(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, _, agent_id, _ = await setup_published_agent(
        client, platform_headers, create_tenant_user, rules=weekly_rules()
    )
    listing = await make_listing(client, admin, agentId=agent_id)
    assert (await transition(client, admin, listing["id"], "published")).status_code == 200

    day = tomorrow()
    resp = await book(
        client,
        slot_at(day, 10),
        email="tourist@example.com",
        listingId=listing["id"],
        message="Can we visit?",
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "requested"
    assert body["endAt"] > body["startAt"]

    detail = await client.get(f"{PORTAL_APPOINTMENTS}/{body['id']}", headers=admin)
    assert detail.status_code == 200, detail.text
    appointment = detail.json()
    assert appointment["agentUserId"] == agent_id
    assert appointment["listingId"] == listing["id"]
    assert appointment["leadId"] is not None

    # The lead is pinned to the booked agent — not routed by the assignment
    # engine — and carries the tour on its timeline.
    lead = (await client.get(f"{PORTAL_LEADS}/{appointment['leadId']}", headers=admin)).json()
    assert lead["source"] == "tour_request"
    assert lead["agentId"] == agent_id
    assert lead["listingId"] == listing["id"]
    acts = (
        await client.get(f"{PORTAL_LEADS}/{appointment['leadId']}/activities", headers=admin)
    ).json()
    tour = [a for a in acts if a["type"] == "tour"]
    assert tour and tour[0]["payload"]["event"] == "tour_requested"

    # The same slot cannot be booked twice; off-grid times are rejected too.
    dup = await book(client, slot_at(day, 10), email="rival@example.com")
    assert dup.status_code == 409
    offgrid = await book(client, slot_at(day, 10) + timedelta(minutes=30), email="x@example.com")
    assert offgrid.status_code == 409


async def test_booking_honeypot_returns_normal_response_but_persists_nothing(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, _, _, _ = await setup_published_agent(
        client, platform_headers, create_tenant_user, rules=weekly_rules()
    )
    resp = await book(client, slot_at(tomorrow(), 9), email="bot@example.com", hp="gotcha")
    assert resp.status_code == 201
    assert resp.json()["status"] == "requested"

    listed = await client.get(PORTAL_APPOINTMENTS, headers=admin)
    assert listed.json()["items"] == []
    leads = await client.get(PORTAL_LEADS, headers=admin)
    assert leads.json()["items"] == []


# ---- portal lifecycle ----


async def test_transitions_email_contact_and_enforce_graph(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, agent, _, _ = await setup_published_agent(
        client, platform_headers, create_tenant_user, rules=weekly_rules()
    )
    email = f"visitor-{uuid.uuid4().hex[:8]}@example.com"
    booked = (await book(client, slot_at(tomorrow(), 9), email=email)).json()

    # The booked agent confirms their own appointment; the contact is emailed.
    resp = await transition_appointment(client, agent, booked["id"], "confirmed")
    assert resp.status_code == 200, resp.text
    assert resp.json()["confirmedAt"] is not None
    assert await mailpit_count(email, "confirmed") == 1

    # Graph: confirmed → confirmed is invalid; back to requested is a 422.
    await assert_transition(client, agent, booked["id"], "confirmed", 409)
    await assert_transition(client, agent, booked["id"], "requested", 422)

    await assert_transition(client, admin, booked["id"], "completed", 200)
    # Terminal: nothing moves out of completed.
    await assert_transition(client, admin, booked["id"], "cancelled", 409)

    # Cancelling a fresh request emails the contact too.
    email2 = f"visitor-{uuid.uuid4().hex[:8]}@example.com"
    booked2 = (await book(client, slot_at(tomorrow(), 10), email=email2)).json()
    await assert_transition(client, admin, booked2["id"], "cancelled", 200)
    assert await mailpit_count(email2, "cancelled") == 1


async def test_no_show_penalizes_lead_score(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, _, _, _ = await setup_published_agent(
        client, platform_headers, create_tenant_user, rules=weekly_rules()
    )
    booked = (await book(client, slot_at(tomorrow(), 9), email="noshow@example.com")).json()
    detail = (await client.get(f"{PORTAL_APPOINTMENTS}/{booked['id']}", headers=admin)).json()
    lead_id = detail["leadId"]

    await assert_transition(client, admin, booked["id"], "confirmed", 200)
    before = (await client.get(f"{PORTAL_LEADS}/{lead_id}", headers=admin)).json()["score"]

    await assert_transition(client, admin, booked["id"], "no_show", 200)
    after = (await client.get(f"{PORTAL_LEADS}/{lead_id}", headers=admin)).json()["score"]
    assert after == before - 15

    acts = (await client.get(f"{PORTAL_LEADS}/{lead_id}/activities", headers=admin)).json()
    assert [a for a in acts if a["type"] == "no_show"]


async def test_portal_visibility_is_ownership_scoped(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin, agent, _, _ = await setup_published_agent(
        client, platform_headers, create_tenant_user, rules=weekly_rules()
    )
    booked = (await book(client, slot_at(tomorrow(), 9), email="scoped@example.com")).json()

    other = await add_user(
        client, create_tenant_user, str(tenant["id"]), Role.AGENT, email="rival@a.example.com"
    )
    assert (await client.get(PORTAL_APPOINTMENTS, headers=other)).json()["items"] == []
    assert (
        await client.get(f"{PORTAL_APPOINTMENTS}/{booked['id']}", headers=other)
    ).status_code == 404
    # And a foreign agent cannot drive the lifecycle either.
    await assert_transition(client, other, booked["id"], "confirmed", 404)

    # Owner and admin both see it; buyers hold no APPOINTMENT_MANAGE at all.
    assert len((await client.get(PORTAL_APPOINTMENTS, headers=agent)).json()["items"]) == 1
    assert len((await client.get(PORTAL_APPOINTMENTS, headers=admin)).json()["items"]) == 1
    buyer = await add_user(
        client,
        create_tenant_user,
        str(tenant["id"]),
        Role.BUYER_RENTER,
        email="buyer2@a.example.com",
    )
    assert (await client.get(PORTAL_APPOINTMENTS, headers=buyer)).status_code == 403


# ---- Beat reminders ----


async def test_reminder_sweep_sends_once_per_window(
    app: FastAPI,
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin, _, _, _ = await setup_published_agent(
        client, platform_headers, create_tenant_user, rules=weekly_rules()
    )
    day = tomorrow()
    soon_email = f"soon-{uuid.uuid4().hex[:8]}@example.com"
    later_email = f"later-{uuid.uuid4().hex[:8]}@example.com"
    soon = (await book(client, slot_at(day, 9), email=soon_email)).json()
    later = (await book(client, slot_at(day, 10), email=later_email)).json()
    for appointment_id in (soon["id"], later["id"]):
        await assert_transition(client, admin, appointment_id, "confirmed", 200)

    # One visit 30 minutes out (1h window), one 10 hours out (24h window only).
    await shift_appointment(app, str(tenant["id"]), soon["id"], starts_in=timedelta(minutes=30))
    await shift_appointment(app, str(tenant["id"]), later["id"], starts_in=timedelta(hours=10))

    result = send_tour_reminders()
    assert result["reminders_sent"] == 2
    assert await mailpit_count(soon_email, "hour") == 1
    assert await mailpit_count(later_email, "tomorrow") == 1

    # The short-notice booking got both stamps at once — one email, not two.
    detail = (await client.get(f"{PORTAL_APPOINTMENTS}/{soon['id']}", headers=admin)).json()
    assert detail["reminder1HSentAt"] is not None
    assert detail["reminder24HSentAt"] is not None

    # Idempotent: a rerun finds nothing due.
    again = send_tour_reminders()
    assert again["reminders_sent"] == 0
    assert await mailpit_count(soon_email, "hour") == 1
    assert await mailpit_count(later_email, "tomorrow") == 1


# ---- iCal feed ----


async def test_ical_feed_secret_url(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin, agent, _, profile = await setup_published_agent(
        client, platform_headers, create_tenant_user, rules=weekly_rules()
    )
    booked = (await book(client, slot_at(tomorrow(), 9), email="cal@example.com")).json()
    await assert_transition(client, admin, booked["id"], "confirmed", 200)

    resp = await client.get(f"{PORTAL_AGENTS}/{profile['id']}/ical", headers=agent)
    assert resp.status_code == 200, resp.text
    url = resp.json()["url"]
    assert url.startswith("/api/v1/appointments/ical/")

    feed = await client.get(url, headers={"Host": HOST_A})
    assert feed.status_code == 200
    assert feed.headers["content-type"].startswith("text/calendar")
    calendar = Calendar.from_ical(feed.content)
    events = [c for c in calendar.walk() if c.name == "VEVENT"]
    assert len(events) == 1
    assert str(events[0]["status"]) == "CONFIRMED"
    assert "Visitor" in str(events[0]["summary"])

    # A tampered token is a plain 404 — no oracle.
    forged = await client.get(url + "0", headers={"Host": HOST_A})
    assert forged.status_code == 404


# ---- buyer-side tour list (/me/appointments) ----

ME_APPOINTMENTS = "/api/v1/me/appointments"


async def _verified_buyer(client: AsyncClient, email: str) -> dict[str, str]:
    """Register a buyer and complete the real email-verification flow — the
    /me tour list joins on a *verified* address, so a fixture-inserted user
    with a NULL emailVerifiedAt would not exercise the happy path."""
    resp = await register_user(client, HOST_A, email=email)
    assert resp.status_code == 201, resp.text
    headers = {"Host": HOST_A, "Authorization": bearer(resp)}
    code = await mailpit_code(email, "Verify")
    verified = await client.post(
        "/api/v1/auth/verify-email", json={"token": code}, headers={"Host": HOST_A}
    )
    assert verified.status_code == 204, verified.text
    return headers


async def test_visitor_sees_own_tours_after_booking(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """A tour booked anonymously shows up in the buyer's account: the join is
    by CRM contact via the verified email, since the booking carries no user id."""
    _, admin, _, agent_id, _ = await setup_published_agent(
        client, platform_headers, create_tenant_user, rules=weekly_rules()
    )
    email = "tour-buyer@example.com"
    buyer = await _verified_buyer(client, email)

    # Nothing booked yet.
    empty = await client.get(ME_APPOINTMENTS, headers=buyer)
    assert empty.status_code == 200, empty.text
    assert empty.json()["items"] == []
    assert empty.json()["totalEstimate"] == 0

    day = tomorrow()
    booked = await book(client, slot_at(day, 10), email=email)
    assert booked.status_code == 201, booked.text

    listed = await client.get(ME_APPOINTMENTS, headers=buyer)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [x["id"] for x in body["items"]] == [booked.json()["id"]]
    assert body["totalEstimate"] == 1

    row = body["items"][0]
    assert row["status"] == "requested"
    assert row["agentUserId"] == agent_id
    # Internal bookkeeping must not reach the visitor.
    # NB the capital H: to_camel renders these as "24H"/"1H", so the lowercase
    # spelling would assert the absence of a field that never existed.
    for leaked in ("contactId", "leadId", "reminder24HSentAt", "reminder1HSentAt"):
        assert leaked not in row, f"{leaked} leaked to the visitor"

    # A confirmation by the agency is reflected on the buyer's side.
    confirmed = await client.post(
        f"{PORTAL_APPOINTMENTS}/{booked.json()['id']}/status",
        json={"toStatus": "confirmed"},
        headers=admin,
    )
    assert confirmed.status_code == 200, confirmed.text
    again = await client.get(ME_APPOINTMENTS, headers=buyer)
    assert again.json()["items"][0]["status"] == "confirmed"
    assert again.json()["items"][0]["confirmedAt"] is not None


async def test_visitor_never_sees_another_persons_tours(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """The list is scoped to the caller's own contact, not the tenant."""
    await setup_published_agent(client, platform_headers, create_tenant_user, rules=weekly_rules())
    mine = "mine@example.com"
    theirs = "theirs@example.com"
    buyer = await _verified_buyer(client, mine)

    day = tomorrow()
    ours = await book(client, slot_at(day, 10), email=mine)
    assert ours.status_code == 201, ours.text
    other = await book(client, slot_at(day, 11), email=theirs)
    assert other.status_code == 201, other.text

    listed = await client.get(ME_APPOINTMENTS, headers=buyer)
    ids = [x["id"] for x in listed.json()["items"]]
    assert ids == [ours.json()["id"]]
    assert other.json()["id"] not in ids


async def test_unverified_email_yields_an_empty_tour_list(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """Anyone can register claiming any address. Joining on an unverified one
    would hand a stranger's tour history to whoever typed their address first,
    so an unverified caller sees nothing — and gets a 200, not a 403, which
    would itself confirm the address is in the CRM."""
    await setup_published_agent(client, platform_headers, create_tenant_user, rules=weekly_rules())
    email = "unverified@example.com"
    booked = await book(client, slot_at(tomorrow(), 10), email=email)
    assert booked.status_code == 201, booked.text

    # Same address, but the account never completed verification.
    resp = await register_user(client, HOST_A, email=email)
    assert resp.status_code == 201, resp.text
    impostor = {"Host": HOST_A, "Authorization": bearer(resp)}

    listed = await client.get(ME_APPOINTMENTS, headers=impostor)
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []
    assert listed.json()["totalEstimate"] == 0


async def test_tour_list_requires_authentication(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await setup_published_agent(client, platform_headers, create_tenant_user)
    anon = await client.get(ME_APPOINTMENTS, headers={"Host": HOST_A})
    assert anon.status_code == 401


async def test_upcoming_only_filters_past_and_dead_tours(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """``upcomingOnly`` answers "what's next": past tours and cancelled ones
    drop out, while the unfiltered list keeps the full history."""
    tenant, admin, _, _, _ = await setup_published_agent(
        client, platform_headers, create_tenant_user, rules=weekly_rules()
    )
    email = "history@example.com"
    buyer = await _verified_buyer(client, email)

    day = tomorrow()
    upcoming = await book(client, slot_at(day, 10), email=email)
    cancelled = await book(client, slot_at(day, 11), email=email)
    past = await book(client, slot_at(day, 9), email=email)
    for resp in (upcoming, cancelled, past):
        assert resp.status_code == 201, resp.text

    # Cancel one, and drag another into the past (booking is future-only).
    killed = await client.post(
        f"{PORTAL_APPOINTMENTS}/{cancelled.json()['id']}/status",
        json={"toStatus": "cancelled"},
        headers=admin,
    )
    assert killed.status_code == 200, killed.text
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, uuid.UUID(str(tenant["id"])))
        await session.execute(
            update(Appointment)
            .where(Appointment.id == uuid.UUID(past.json()["id"]))
            .values(
                start_at=datetime.now(UTC) - timedelta(days=2),
                end_at=datetime.now(UTC) - timedelta(days=2) + timedelta(hours=1),
            )
        )

    filtered = await client.get(ME_APPOINTMENTS, params={"upcomingOnly": True}, headers=buyer)
    assert [x["id"] for x in filtered.json()["items"]] == [upcoming.json()["id"]]
    assert filtered.json()["totalEstimate"] == 1

    everything = await client.get(ME_APPOINTMENTS, headers=buyer)
    assert len(everything.json()["items"]) == 3
    # Ordered by start_at ascending — the past one leads the full history.
    assert everything.json()["items"][0]["id"] == past.json()["id"]


async def test_tour_list_token_is_pinned_to_its_tenant(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """An access token minted on agency A cannot read the tour list on B."""
    await setup_published_agent(client, platform_headers, create_tenant_user, rules=weekly_rules())
    email = "cross@example.com"
    buyer = await _verified_buyer(client, email)
    booked = await book(client, slot_at(tomorrow(), 10), email=email)
    assert booked.status_code == 201, booked.text

    mine = await client.get(ME_APPOINTMENTS, headers=buyer)
    assert [x["id"] for x in mine.json()["items"]] == [booked.json()["id"]]

    await create_tenant(client, platform_headers, name="Agency B", slug="agency-b", domain=HOST_B)
    refused = await client.get(
        ME_APPOINTMENTS, headers={"Host": HOST_B, "Authorization": buyer["Authorization"]}
    )
    assert refused.status_code in (401, 403)
