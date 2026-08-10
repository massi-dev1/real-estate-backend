"""Seed a demo tenant for local frontend development.

Usage:
    uv run python scripts/seed_demo.py
    uv run python scripts/seed_demo.py --reset   # delete the tenant first

Creates the `demo` tenant on `demo.localhost` plus staff, agent profiles, a
team, listings across every workflow status, leads across every stage, tours
and a deal. Browse the frontend at http://demo.localhost:3000 — plain
`localhost` resolves to no tenant and renders the "site not found" screen.

Sign in with `admin@demo.example` / `DemoPass123!` (see STAFF for the other
roles). Note the login addresses use `demo.example`, not the tenant hostname —
see DEMO_EMAIL_DOMAIN for why.

Idempotent: re-running skips whatever already exists, so it is safe to run
after a partial failure. Use --reset to start from a clean tenant.

Everything goes through the real **service layer** rather than hand-built ORM
rows, so the seeded data satisfies the same invariants production data does:
reference codes are minted by the counter, status changes append history,
lead scores are computed, and assignment rules actually run. A hand-rolled
INSERT would drift from those rules the moment one of them changes.
"""

import argparse
import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import create_engine, create_session_factory, set_tenant_guc
from app.core.permissions import AuthenticatedUser, Role
from app.core.storage import create_storage
from app.core.tenancy import TenantContext
from app.modules.agents.repository import AgentsRepository
from app.modules.agents.schemas import AgentProfileCreate, TeamCreate, TeamMemberAdd
from app.modules.agents.service import AgentsService
from app.modules.appointments.models import AppointmentStatus
from app.modules.leads.models import ActivityType, LeadSource, LeadStage
from app.modules.leads.schemas import ActivityCreate, ContactCaptureIn, LeadCreate, LeadFilters
from app.modules.leads.service import get_leads_service
from app.modules.listings.models import ListingPurpose, ListingStatus, PropertyType
from app.modules.listings.schemas import AddressIn, ListingCreate, PointIn
from app.modules.listings.service import get_listing_service

# `users.tenant_id` carries an FK to `tenants`, which SQLAlchemy resolves lazily
# on first use. Importing the users module alone leaves `tenants` absent from
# Base.metadata and the mapper raises NoReferencedTableError, so pull in the
# model registry (which imports every module's models) before the repositories.
from app.modules.tenants import models as _tenant_models  # noqa: F401
from app.modules.tenants.models import TenantStatus
from app.modules.tenants.repository import TenantRepository
from app.modules.tenants.schemas import TenantCreate
from app.modules.tenants.service import TenantService, _domain_cache_key
from app.modules.tenants.usage import build_usage_boundary
from app.modules.transactions.repository import TransactionsRepository
from app.modules.transactions.schemas import DealCreate
from app.modules.transactions.service import TransactionsService
from app.modules.users.repository import UserRepository
from app.modules.users.service import UserService

DEMO_SLUG = "demo"
# The tenant hostname. *.localhost resolves to 127.0.0.1 in Node and every
# browser, so no hosts-file entry is needed.
DEMO_DOMAIN = "demo.localhost"
# Login emails deliberately do NOT use the hostname. pydantic's EmailStr
# (email-validator) refuses special-use names, so an `admin@demo.localhost`
# account is created fine but can never log in — the login body 422s before it
# ever reaches the password check. Verified empirically: `.localhost`, `.test`
# and `.local` are all rejected; `.example` is accepted. That makes
# `demo.example` the right pick — reserved by RFC 2606 so it can never collide
# with a real inbox, yet valid to the validator.
DEMO_EMAIL_DOMAIN = "demo.example"
DEMO_NAME = "Demo Immobilier"
DEMO_PLAN = "growth"
# Dev-only. The backend rejects short passwords; this is never a prod path.
DEMO_PASSWORD = "DemoPass123!"

# role → (email local part, first, last)
STAFF: list[tuple[Role, str, str, str]] = [
    (Role.ADMIN, "admin", "Amina", "Belkacem"),
    (Role.TEAM_LEAD, "lead", "Karim", "Hadj"),
    (Role.AGENT, "agent", "Sofiane", "Meziane"),
    (Role.AGENT, "agent2", "Lina", "Cherif"),
    (Role.MARKETING, "marketing", "Yacine", "Boudiaf"),
]

# Platform staff live *outside* every tenant (`tenant_id IS NULL`) and sign in
# on a separate auth plane, so the console is unreachable without one. Seeded
# here rather than left to `create_platform_admin.py` so a single command gives
# a working environment for both surfaces.
PLATFORM_EMAIL = f"platform@{DEMO_EMAIL_DOMAIN}"
PLATFORM_PASSWORD = "PlatformPass123!"


def _log(message: str) -> None:
    print(f"  {message}")


# --------------------------------------------------------------------------
# actors
#
# Services take (TenantContext, AuthenticatedUser) exactly as a request would.
# The jti is unused off the request path (it only matters for token
# revocation), so a fresh uuid keeps the dataclass honest without inventing a
# fake session.
# --------------------------------------------------------------------------
def _actor(tenant_id: uuid.UUID, user_id: uuid.UUID, role: Role) -> AuthenticatedUser:
    return AuthenticatedUser(id=user_id, tenant_id=tenant_id, role=role, jti=str(uuid.uuid4()))


def _context(tenant: Any) -> TenantContext:
    return TenantContext(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        status=tenant.status.value if hasattr(tenant.status, "value") else str(tenant.status),
        settings=tenant.settings or {},
        plan=tenant.plan,
    )


# --------------------------------------------------------------------------
# tenant + users
# --------------------------------------------------------------------------
async def _ensure_tenant(session: AsyncSession) -> Any:
    """The tenants table is deliberately NOT RLS-protected (the resolution
    middleware queries it before any tenant context exists), so this runs on a
    plain session."""
    repo = TenantRepository(session)
    existing = await repo.get_by_slug(DEMO_SLUG)
    if existing is not None:
        _log(f"tenant '{DEMO_SLUG}' already exists ({existing.id})")
        return existing

    # redis=None: every write path here invalidates the domain cache through a
    # post-commit callback, and this script never registers one (no request
    # boundary to drain it). Nothing reads the cache in-process either.
    service = TenantService(repo, redis=None)  # type: ignore[arg-type]
    tenant = await service.create(
        TenantCreate(
            name=DEMO_NAME,
            slug=DEMO_SLUG,
            domain=DEMO_DOMAIN,
            settings={
                "contact": {"whatsapp_number": "+213555000000", "email": "contact@demo.example"},
                "listings": {"agent_self_publish": True},
                "appointments": {"timezone": "Africa/Algiers", "slot_minutes": 60},
            },
        )
    )
    # A fresh tenant lands on the trial plan and a trial clock. The demo should
    # behave like a paying customer: a lapsed trial would suspend it mid-demo
    # (the expire-trials sweep 402s the site) and the trial quota is too tight
    # for the listing count below.
    await service.set_plan(tenant.id, DEMO_PLAN)
    await service.set_status(tenant.id, TenantStatus.ACTIVE)
    tenant.trial_ends_at = None
    await session.flush()
    _log(f"created tenant '{DEMO_SLUG}' on {DEMO_DOMAIN} (plan={DEMO_PLAN})")
    return tenant


async def _ensure_users(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    """Users are RLS-protected by the identity policy, so the tenant GUC must
    be set before touching them (the caller does that)."""
    service = UserService(UserRepository(session))
    users: dict[str, Any] = {}
    for role, local, first, last in STAFF:
        email = f"{local}@{DEMO_EMAIL_DOMAIN}"
        existing = await service.repo.get_by_email(tenant_id, email)
        if existing is not None:
            users[local] = existing
            continue
        user = await service.create_account(
            tenant_id,
            email=email,
            password=DEMO_PASSWORD,
            role=role,
            first_name=first,
            last_name=last,
            locale="fr",
        )
        # Verified inbox: /me/appointments joins the CRM on a *verified* email
        # only, so an unverified demo user would see an empty tour list and
        # look broken rather than secure.
        user.email_verified_at = datetime.now(UTC)
        users[local] = user
        _log(f"user {email} ({role.value})")
    await session.flush()
    return users


async def _ensure_platform_admin(session: AsyncSession) -> None:
    """Create the platform-console operator.

    Deliberately *not* tenant-scoped: `tenant_id` is NULL, which is what makes
    an account platform staff. The identity RLS policy exposes exactly the
    caller's partition, so this runs in the unscoped part of the seed — with a
    tenant GUC set, the lookup below would not see an existing platform row and
    every re-run would try to insert a duplicate.
    """
    service = UserService(UserRepository(session))
    existing = await service.repo.get_by_email(None, PLATFORM_EMAIL)
    if existing is not None:
        return
    await service.create_account(
        None,
        email=PLATFORM_EMAIL,
        password=PLATFORM_PASSWORD,
        role=Role.PLATFORM_ADMIN,
        first_name="Sarah",
        last_name="Idir",
        locale="fr",
    )
    await session.flush()
    _log(f"platform admin {PLATFORM_EMAIL}")


# --------------------------------------------------------------------------
# agents + team
# --------------------------------------------------------------------------
AGENT_SEED: list[tuple[str, str, list[str], str]] = [
    ("lead", "karim-hadj", ["residential_sales", "luxury"], "+213555111111"),
    ("agent", "sofiane-meziane", ["residential_sales", "new_developments"], "+213555222222"),
    ("agent2", "lina-cherif", ["residential_rentals", "commercial"], "+213555333333"),
]


async def _ensure_agents(
    session: AsyncSession, ctx: TenantContext, users: dict[str, Any]
) -> dict[str, Any]:
    repo = AgentsRepository(session)
    # Not build_agents_boundary(): that constructs a deliberately usage-free
    # AgentsService for dependent modules, and create_profile reserves a plan
    # seat. Storage/settings stay None — only the photo pipeline needs them.
    service = AgentsService(
        repo,
        UserService(UserRepository(session)),
        usage=build_usage_boundary(session),
    )
    admin = _actor(ctx.id, users["admin"].id, Role.ADMIN)
    profiles: dict[str, Any] = {}

    for key, slug, specialties, whatsapp in AGENT_SEED:
        user = users[key]
        existing = await repo.get_by_user(ctx.id, user.id)
        if existing is not None:
            profiles[key] = existing
            continue
        profile = await service.create_profile(
            ctx,
            admin,
            AgentProfileCreate(
                user_id=user.id,
                slug=slug,
                bio={
                    "fr": f"{user.first_name} accompagne ses clients à Alger depuis 2015.",
                    "en": f"{user.first_name} has been advising clients in Algiers since 2015.",
                },
                specialties=specialties,
                whatsapp_number=whatsapp,
            ),
        )
        # Published: the public directory and the review/booking flows only
        # resolve published profiles, so an unpublished seed makes those 404.
        profile.is_published = True
        profiles[key] = profile
        _log(f"agent profile /{slug}")

    await session.flush()

    teams = await service.list_teams(ctx, admin)
    if not teams:
        team = await service.create_team(
            ctx, admin, TeamCreate(name="Équipe Alger Centre", lead_user_id=users["lead"].id)
        )
        lead_actor = _actor(ctx.id, users["lead"].id, Role.TEAM_LEAD)
        for key in ("agent", "agent2"):
            await service.add_member(ctx, lead_actor, team.id, TeamMemberAdd(user_id=users[key].id))
        _log("team 'Équipe Alger Centre' with 2 members")
        # Membership widens the team lead's visibility scope — that is exactly
        # what the portal's scoping behaviour needs to be demonstrable.
    await session.flush()
    return profiles


# --------------------------------------------------------------------------
# listings
# --------------------------------------------------------------------------
# (title_fr, title_en, purpose, type, price, beds, baths, area, city, lat, lng,
#  features, target_status, owner_key)
LISTING_SEED: list[tuple[Any, ...]] = [
    (
        "Villa avec piscine à Hydra",
        "Villa with pool in Hydra",
        ListingPurpose.SALE,
        PropertyType.VILLA,
        "78000000",
        5,
        3,
        "420",
        "Alger",
        36.7450,
        3.0300,
        ["pool", "garden", "garage", "security"],
        ListingStatus.PUBLISHED,
        "agent",
    ),
    (
        "Appartement F3 à Bab Ezzouar",
        "F3 apartment in Bab Ezzouar",
        ListingPurpose.SALE,
        PropertyType.APARTMENT,
        "14500000",
        3,
        1,
        "95",
        "Alger",
        36.7200,
        3.1830,
        ["elevator", "balcony", "parking"],
        ListingStatus.PUBLISHED,
        "agent",
    ),
    (
        "Duplex vue mer à Oran",
        "Sea-view duplex in Oran",
        ListingPurpose.SALE,
        PropertyType.DUPLEX,
        "32000000",
        4,
        2,
        "180",
        "Oran",
        35.7000,
        -0.6500,
        ["sea_view", "terrace", "air_conditioning"],
        ListingStatus.PUBLISHED,
        "agent2",
    ),
    (
        "Studio meublé à Sidi Yahia",
        "Furnished studio in Sidi Yahia",
        ListingPurpose.RENT,
        PropertyType.STUDIO,
        "65000",
        1,
        1,
        "45",
        "Alger",
        36.7420,
        3.0400,
        ["furnished", "air_conditioning", "fiber_internet"],
        ListingStatus.PUBLISHED,
        "agent2",
    ),
    (
        "Local commercial Didouche Mourad",
        "Retail unit on Didouche Mourad",
        ListingPurpose.RENT,
        PropertyType.RETAIL,
        "180000",
        None,
        1,
        "120",
        "Alger",
        36.7680,
        3.0570,
        ["security"],
        ListingStatus.PUBLISHED,
        "lead",
    ),
    (
        "Terrain constructible à Tipaza",
        "Building land in Tipaza",
        ListingPurpose.SALE,
        PropertyType.LAND,
        "22000000",
        None,
        None,
        "800",
        "Tipaza",
        36.5900,
        2.4500,
        [],
        ListingStatus.PUBLISHED,
        "lead",
    ),
    (
        "Appartement F4 à Constantine",
        "F4 apartment in Constantine",
        ListingPurpose.SALE,
        PropertyType.APARTMENT,
        "19800000",
        4,
        2,
        "130",
        "Constantine",
        36.3650,
        6.6147,
        ["elevator", "heating", "balcony"],
        ListingStatus.PUBLISHED,
        "agent",
    ),
    (
        "Villa moderne à Annaba",
        "Modern villa in Annaba",
        ListingPurpose.SALE,
        PropertyType.VILLA,
        "45000000",
        4,
        3,
        "300",
        "Annaba",
        36.9000,
        7.7667,
        ["garden", "garage", "solar_panels"],
        ListingStatus.RESERVED,
        "agent2",
    ),
    (
        "Bureau open-space à Chéraga",
        "Open-plan office in Cheraga",
        ListingPurpose.RENT,
        PropertyType.OFFICE,
        "250000",
        None,
        2,
        "200",
        "Alger",
        36.7600,
        2.9600,
        ["parking", "fiber_internet", "elevator"],
        ListingStatus.REVIEW,
        "agent",
    ),
    (
        "Maison de campagne à Blida",
        "Country house in Blida",
        ListingPurpose.SALE,
        PropertyType.HOUSE,
        "16500000",
        3,
        2,
        "160",
        "Blida",
        36.4700,
        2.8300,
        ["garden", "mountain_view"],
        ListingStatus.DRAFT,
        "agent2",
    ),
    (
        "Hangar industriel à Rouiba",
        "Industrial warehouse in Rouiba",
        ListingPurpose.RENT,
        PropertyType.WAREHOUSE,
        "400000",
        None,
        1,
        "1200",
        "Alger",
        36.7380,
        3.2860,
        ["parking"],
        ListingStatus.DRAFT,
        "lead",
    ),
    (
        "Appartement F2 vendu à Kouba",
        "F2 apartment sold in Kouba",
        ListingPurpose.SALE,
        PropertyType.APARTMENT,
        "11000000",
        2,
        1,
        "70",
        "Alger",
        36.7180,
        3.0870,
        ["balcony"],
        ListingStatus.SOLD,
        "agent",
    ),
]

# The workflow graph forbids jumping straight to a terminal state, so walk the
# same path a real agent would. Publishing is what stamps published_at and
# fires the search-alert / syndication hooks.
STATUS_PATH: dict[ListingStatus, list[ListingStatus]] = {
    ListingStatus.DRAFT: [],
    ListingStatus.REVIEW: [ListingStatus.REVIEW],
    ListingStatus.PUBLISHED: [ListingStatus.REVIEW, ListingStatus.PUBLISHED],
    ListingStatus.RESERVED: [
        ListingStatus.REVIEW,
        ListingStatus.PUBLISHED,
        ListingStatus.RESERVED,
    ],
    ListingStatus.SOLD: [
        ListingStatus.REVIEW,
        ListingStatus.PUBLISHED,
        ListingStatus.RESERVED,
        ListingStatus.SOLD,
    ],
}


async def _ensure_listings(
    session: AsyncSession, ctx: TenantContext, users: dict[str, Any]
) -> list[Any]:
    service = get_listing_service(session)
    admin = _actor(ctx.id, users["admin"].id, Role.ADMIN)

    existing, _, total = await service.list_portal(ctx, admin, status=None, cursor=None, limit=100)
    if total >= len(LISTING_SEED):
        _log(f"{total} listings already present")
        return existing

    created: list[Any] = []
    for row in LISTING_SEED:
        (
            title_fr,
            title_en,
            purpose,
            ptype,
            price,
            beds,
            baths,
            area,
            city,
            lat,
            lng,
            features,
            target,
            owner_key,
        ) = row
        listing = await service.create(
            ctx,
            admin,
            ListingCreate(
                purpose=purpose,
                property_type=ptype,
                title={"fr": title_fr, "en": title_en},
                description={
                    "fr": f"{title_fr}. Bien proposé par {DEMO_NAME}, disponible à la visite.",
                    "en": f"{title_en}. Offered by {DEMO_NAME}, available for viewing.",
                },
                price=Decimal(price),
                currency="DZD",
                beds=beds,
                baths=baths,
                area_built=Decimal(area),
                features=features,
                address=AddressIn(city=city, country="DZ"),
                location=PointIn(lat=lat, lng=lng),
                agent_id=users[owner_key].id,
            ),
        )
        for step in STATUS_PATH[target]:
            await service.transition(ctx, admin, listing.id, step)
        created.append(listing)
    await session.flush()
    _log(f"{len(created)} listings across draft/review/published/reserved/sold")
    return created


# --------------------------------------------------------------------------
# leads
# --------------------------------------------------------------------------
# (first, last, email, phone, source, target_stage, listing_index, lost_reason)
LEAD_SEED: list[tuple[Any, ...]] = [
    (
        "Nadia",
        "Saïdi",
        "nadia.saidi@example.com",
        "+213661000001",
        LeadSource.LISTING_FORM,
        LeadStage.NEW,
        0,
        None,
    ),
    (
        "Omar",
        "Benali",
        "omar.benali@example.com",
        "+213661000002",
        LeadSource.WHATSAPP_CLICK,
        LeadStage.CONTACTED,
        1,
        None,
    ),
    (
        "Feriel",
        "Zerrouki",
        "feriel.z@example.com",
        "+213661000003",
        LeadSource.TOUR_REQUEST,
        LeadStage.TOURING,
        2,
        None,
    ),
    (
        "Reda",
        "Aït Ali",
        "reda.aitali@example.com",
        "+213661000004",
        LeadSource.PHONE,
        LeadStage.QUALIFIED,
        0,
        None,
    ),
    (
        "Samira",
        "Lounis",
        "samira.lounis@example.com",
        "+213661000005",
        LeadSource.VALUATION,
        LeadStage.NEW,
        None,
        None,
    ),
    (
        "Yanis",
        "Kaci",
        "yanis.kaci@example.com",
        "+213661000006",
        LeadSource.PORTAL,
        LeadStage.OFFER,
        6,
        None,
    ),
    (
        "Meriem",
        "Brahimi",
        "meriem.b@example.com",
        "+213661000007",
        LeadSource.SEARCH_SIGNUP,
        LeadStage.CONTACTED,
        None,
        None,
    ),
    (
        "Tarek",
        "Guerroudj",
        "tarek.g@example.com",
        "+213661000008",
        LeadSource.AD,
        LeadStage.LOST,
        3,
        "Budget insuffisant",
    ),
    (
        "Hafida",
        "Amrani",
        "hafida.amrani@example.com",
        "+213661000009",
        LeadSource.MORTGAGE,
        LeadStage.NEW,
        None,
        None,
    ),
    (
        "Bilal",
        "Ould Kada",
        "bilal.ok@example.com",
        "+213661000010",
        LeadSource.LISTING_FORM,
        LeadStage.WON,
        11,
        None,
    ),
    (
        "Souad",
        "Ferhat",
        "souad.ferhat@example.com",
        "+213661000011",
        LeadSource.MARKET_REPORT,
        LeadStage.NEW,
        None,
        None,
    ),
    (
        "Djamel",
        "Slimani",
        "djamel.slimani@example.com",
        "+213661000012",
        LeadSource.PHONE,
        LeadStage.CONTACTED,
        4,
        None,
    ),
    (
        "Lynda",
        "Hamidi",
        "lynda.hamidi@example.com",
        "+213661000013",
        LeadSource.LISTING_FORM,
        LeadStage.QUALIFIED,
        5,
        None,
    ),
    (
        "Rachid",
        "Boukerche",
        "rachid.b@example.com",
        "+213661000014",
        LeadSource.OTHER,
        LeadStage.LOST,
        None,
        "Ne répond plus",
    ),
    (
        "Nawel",
        "Tounsi",
        "nawel.tounsi@example.com",
        "+213661000015",
        LeadSource.WHATSAPP_CLICK,
        LeadStage.TOURING,
        7,
        None,
    ),
]

# Reaching a stage means passing through the ones before it — the timeline
# should read like a real pipeline, not a single jump.
STAGE_PATH: dict[LeadStage, list[LeadStage]] = {
    LeadStage.NEW: [],
    LeadStage.CONTACTED: [LeadStage.CONTACTED],
    LeadStage.QUALIFIED: [LeadStage.CONTACTED, LeadStage.QUALIFIED],
    LeadStage.TOURING: [LeadStage.CONTACTED, LeadStage.QUALIFIED, LeadStage.TOURING],
    LeadStage.OFFER: [
        LeadStage.CONTACTED,
        LeadStage.QUALIFIED,
        LeadStage.TOURING,
        LeadStage.OFFER,
    ],
    LeadStage.WON: [
        LeadStage.CONTACTED,
        LeadStage.QUALIFIED,
        LeadStage.TOURING,
        LeadStage.OFFER,
        LeadStage.WON,
    ],
    LeadStage.LOST: [LeadStage.CONTACTED, LeadStage.LOST],
}


async def _ensure_leads(
    session: AsyncSession,
    ctx: TenantContext,
    users: dict[str, Any],
    listings: Sequence[Any],
) -> list[Any]:
    service = get_leads_service(session)
    admin = _actor(ctx.id, users["admin"].id, Role.ADMIN)

    _, _, total = await service.list_portal(ctx, admin, filters=LeadFilters(), cursor=None, limit=1)
    if total >= len(LEAD_SEED):
        _log(f"{total} leads already present")
        return []

    created: list[Any] = []
    for first, last, email, phone, source, target, listing_idx, lost_reason in LEAD_SEED:
        listing_id = listings[listing_idx].id if listing_idx is not None else None
        lead = await service.create_manual(
            ctx,
            admin,
            LeadCreate(
                contact=ContactCaptureIn(
                    first_name=first,
                    last_name=last,
                    email=email,
                    phone=phone,
                    marketing_consent=True,
                ),
                listing_id=listing_id,
                source=source,
            ),
        )
        # An agent touch before the stage moves: this is what stamps
        # first_response_at and stops the drip, so the response-time metric on
        # the agent stats page has something real to report.
        if target is not LeadStage.NEW:
            await service.record_activity(
                ctx,
                admin,
                lead.id,
                ActivityCreate(
                    type=ActivityType.CALL,
                    payload={"note": f"Premier appel — {first} {last}."},
                ),
            )
        for step in STAGE_PATH[target]:
            reason = lost_reason if step is LeadStage.LOST else None
            await service.transition_stage(ctx, admin, lead.id, step, reason)
        created.append(lead)

    await session.flush()
    _log(f"{len(created)} leads across new/contacted/qualified/touring/offer/won/lost")
    return created


# --------------------------------------------------------------------------
# appointments + deal
#
# Tours are inserted directly rather than through the public booking endpoint:
# that path derives slots from the agent's availability template and rejects
# any start that is not exactly on a computed slot, which would make the seed
# depend on what weekday it happens to run. The rows are what the portal
# agenda reads.
# --------------------------------------------------------------------------
async def _ensure_appointments(
    session: AsyncSession,
    ctx: TenantContext,
    users: dict[str, Any],
    listings: Sequence[Any],
    leads: Sequence[Any],
) -> None:
    from app.modules.appointments.models import Appointment

    count = await session.scalar(
        text("SELECT count(*) FROM appointments WHERE tenant_id = :t"), {"t": str(ctx.id)}
    )
    if count:
        _log(f"{count} appointments already present")
        return
    if not leads:
        return

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    plan: list[tuple[int, int, AppointmentStatus, str]] = [
        (1, 10, AppointmentStatus.CONFIRMED, "agent"),
        (2, 14, AppointmentStatus.CONFIRMED, "agent2"),
        (3, 11, AppointmentStatus.REQUESTED, "agent"),
        (-2, 15, AppointmentStatus.COMPLETED, "agent2"),
        (-5, 9, AppointmentStatus.NO_SHOW, "agent"),
    ]
    for i, (day_offset, hour, status, agent_key) in enumerate(plan):
        lead = leads[i % len(leads)]
        start = (now + timedelta(days=day_offset)).replace(hour=hour)
        session.add(
            Appointment(
                tenant_id=ctx.id,
                agent_user_id=users[agent_key].id,
                listing_id=listings[i % len(listings)].id,
                contact_id=lead.contact_id,
                lead_id=lead.id,
                status=status,
                start_at=start,
                end_at=start + timedelta(hours=1),
                confirmed_at=(
                    start - timedelta(days=1)
                    if status
                    in (
                        AppointmentStatus.CONFIRMED,
                        AppointmentStatus.COMPLETED,
                        AppointmentStatus.NO_SHOW,
                    )
                    else None
                ),
            )
        )
    await session.flush()
    _log(f"{len(plan)} appointments (past and upcoming)")


async def _ensure_deal(
    session: AsyncSession,
    ctx: TenantContext,
    users: dict[str, Any],
    listings: Sequence[Any],
    leads: Sequence[Any],
) -> None:
    service = TransactionsService(
        TransactionsRepository(session),
        UserService(UserRepository(session)),
        AgentsService(AgentsRepository(session), UserService(UserRepository(session))),
        get_listing_service(session),
        get_leads_service(session),
        # Constructing the client opens no connection; the seed creates no deal
        # documents, so nothing here ever reaches object storage.
        create_storage(get_settings()),
    )
    admin = _actor(ctx.id, users["admin"].id, Role.ADMIN)
    _, _, total = await service.list_deals(ctx, admin, status=None, cursor=None, limit=1)
    if total:
        _log(f"{total} deals already present")
        return
    if not leads:
        return

    won = next((lead for lead in leads if lead.stage is LeadStage.WON), leads[0])
    sold = next((li for li in listings if li.status is ListingStatus.SOLD), listings[0])
    await service.create_deal(
        ctx,
        admin,
        DealCreate(
            title="Vente F2 Kouba — Bilal Ould Kada",
            listing_id=sold.id,
            lead_id=won.id,
            contact_id=won.contact_id,
            owner_user_id=users["agent"].id,
            price=Decimal("11000000"),
            currency="DZD",
            notes="Compromis signé, financement en cours.",
            seed_milestones=True,
        ),
    )
    await session.flush()
    _log("1 deal with the default milestone checklist")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
async def _reset(session: AsyncSession) -> list[str]:
    """Drop the tenant; every owned row cascades from the FK.

    Returns the domains that pointed at it so the caller can evict them from
    the resolver cache.
    """
    domains = list(
        (
            await session.execute(
                text(
                    "SELECT domain FROM tenant_domains WHERE tenant_id IN"
                    " (SELECT id FROM tenants WHERE slug = :slug)"
                ),
                {"slug": DEMO_SLUG},
            )
        ).scalars()
    )
    result = await session.execute(
        text("DELETE FROM tenants WHERE slug = :slug RETURNING id"), {"slug": DEMO_SLUG}
    )
    if result.first() is not None:
        _log(f"deleted existing tenant '{DEMO_SLUG}'")
    return domains


async def _evict_tenant_cache(domains: Sequence[str]) -> None:
    """Drop the Redis domain→tenant mapping for the deleted tenant.

    Deleting the row in SQL bypasses the post-commit invalidation the tenant
    service normally performs, so a long-running API process would keep
    resolving `demo.localhost` to the *old* tenant id and every login would
    401 against users that no longer exist. Best-effort: a cold cache or an
    unreachable Redis is not a reason to fail the seed.
    """
    if not domains:
        return
    settings = get_settings()
    try:
        from redis.asyncio import Redis

        redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
        try:
            await redis.delete(*[_domain_cache_key(d) for d in domains])
            _log(f"evicted {len(domains)} domain(s) from the resolver cache")
        finally:
            await redis.aclose()
    except Exception as exc:  # pragma: no cover - dev convenience
        _log(f"could not evict the tenant cache ({exc}); restart the API if logins 401")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete the demo tenant (and all its data) before seeding",
    )
    args = parser.parse_args()

    engine = create_engine(get_settings())
    session_factory = create_session_factory(engine)
    try:
        if args.reset:
            async with session_factory() as session, session.begin():
                stale_domains = await _reset(session)
            await _evict_tenant_cache(stale_domains)

        print(f"Seeding '{DEMO_SLUG}' ({DEMO_DOMAIN})")
        # Platform staff first, in their own transaction: the block below sets
        # the tenant GUC, and under it the identity RLS policy would hide an
        # existing platform row — making the re-run try to insert a duplicate.
        async with session_factory() as session, session.begin():
            await _ensure_platform_admin(session)

        async with session_factory() as session, session.begin():
            tenant = await _ensure_tenant(session)
            ctx = _context(tenant)
            # Everything past this point touches RLS-protected tables.
            await set_tenant_guc(session, ctx.id)
            users = await _ensure_users(session, ctx.id)
            await _ensure_agents(session, ctx, users)
            listings = await _ensure_listings(session, ctx, users)
            leads = await _ensure_leads(session, ctx, users, listings)
            await _ensure_appointments(session, ctx, users, listings, leads)
            await _ensure_deal(session, ctx, users, listings, leads)
    finally:
        await engine.dispose()

    print(
        "\nDone. Browse http://demo.localhost:3000 (not localhost — that "
        "resolves to no tenant).\nSign in with any of:"
    )
    for role, local, _first, _last in STAFF:
        account = f"{local}@{DEMO_EMAIL_DOMAIN}"
        print(f"  {account:<26} {DEMO_PASSWORD}   ({role.value})")
    print(
        "\nPlatform console: http://localhost:3000/platform/login (bare host — it is tenant-exempt)"
    )
    print(f"  {PLATFORM_EMAIL:<26} {PLATFORM_PASSWORD}   (platform_admin)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
