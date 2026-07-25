"""Model factories for the core entities (§13 factory_boy).

**Why the split shape.** ``factory_boy``'s ``SQLAlchemyModelFactory`` drives a
*synchronous* session, and every session in this codebase is async — worse,
every tenant-owned insert has to run with the ``app.tenant_id`` GUC set or RLS
fails it closed. So the factories here declare **attributes only**
(``factory.Factory`` over a plain dict), which is where factory_boy actually
earns its keep — ``Sequence`` for collision-free slugs and emails, ``Faker``
with a pinned seed for deterministic-but-varied values, ``SubFactory``/traits
for related shapes — and the thin ``insert_*`` helpers below do the RLS-aware
async persist.

**Deterministic seeds (§13).** ``faker`` is seeded at import, so a failing run
is reproducible: the same test produces the same "random" name every time.

**Relationship to the existing ``make_*`` helpers.** These are additive, per
the part's own scoping. The API-driven helpers in ``test_listings`` etc. go
through the *router*, so they exercise validation, workflow and the
service-minted reference code — that is the right tool for a feature test. A
factory writes the row directly, which is the right tool when the row is
*setup*, not the thing under test (the tenant-isolation harness below needs
one object per resource type and does not care how it was made). Neither
replaces the other, and no suite was rewritten wholesale to use these.
"""

import uuid
from typing import Any

import factory
import factory.random
from faker import Faker
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import set_tenant_guc
from app.core.permissions import Role
from app.modules.leads.models import Contact, Lead, LeadSource, LeadStage
from app.modules.listings.models import (
    Listing,
    ListingPurpose,
    ListingStatus,
    PropertyType,
)
from app.modules.tenants.models import Tenant, TenantDomain, TenantStatus
from app.modules.transactions.models import Deal, DealStatus
from app.modules.users.models import User

# Deterministic seeds: a failure reproduces with the same generated values.
FAKER_SEED = 20260725
Faker.seed(FAKER_SEED)
factory.random.reseed_random(FAKER_SEED)


class TenantFactory(factory.DictFactory):
    name = factory.Sequence(lambda n: f"Agency {n}")
    slug = factory.Sequence(lambda n: f"agency-{n}")
    status = TenantStatus.ACTIVE
    plan = "trial"
    settings: dict[str, Any] = {}


class TenantDomainFactory(factory.DictFactory):
    domain = factory.Sequence(lambda n: f"agency-{n}.test")
    is_primary = True


class UserFactory(factory.DictFactory):
    email = factory.Sequence(lambda n: f"user-{n}@example.com")
    role = Role.AGENT
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")


class ListingFactory(factory.DictFactory):
    """A listing row.

    ``reference_code`` is normally minted by the service's per-tenant counter;
    a factory-made row is setup, not a reference-code test, so it gets a
    unique sequence value rather than reaching into the counter table.
    """

    reference_code = factory.Sequence(lambda n: f"FAC-2026-{n:05d}")
    status = ListingStatus.DRAFT
    purpose = ListingPurpose.SALE
    property_type = PropertyType.APARTMENT
    title = factory.Sequence(lambda n: {"fr": f"Appartement {n}"})
    description: dict[str, Any] = {"fr": "Lumineux, proche du centre."}
    price = factory.Sequence(lambda n: 10_000_000 + n)
    currency = "DZD"
    beds = 3
    baths = 1
    features: list[str] = ["balcony"]
    address: dict[str, Any] = {"city": "Alger", "country": "DZ"}


class ContactFactory(factory.DictFactory):
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    email = factory.Sequence(lambda n: f"contact-{n}@example.com")
    phone = factory.Sequence(lambda n: f"+2135550{n:05d}")


class LeadFactory(factory.DictFactory):
    source = LeadSource.LISTING_FORM
    stage = LeadStage.NEW
    score = 10


class DealFactory(factory.DictFactory):
    title = factory.Sequence(lambda n: f"Deal {n}")
    status = DealStatus.OPEN


# ---- async persist helpers (RLS-aware) ----


async def insert_tenant(
    session: AsyncSession, *, domain: str | None = None, **overrides: Any
) -> Tenant:
    """A tenant plus its primary domain.

    ``tenants``/``tenant_domains`` are deliberately global (no RLS — the
    tenant middleware queries them *before* a tenant context exists), so no
    GUC is needed here.
    """
    tenant = Tenant(**TenantFactory(**overrides))
    session.add(tenant)
    await session.flush()
    session.add(
        TenantDomain(
            tenant_id=tenant.id,
            **TenantDomainFactory(**({"domain": domain} if domain else {})),
        )
    )
    await session.flush()
    return tenant


async def insert_user(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    password_hash: str,
    role: Role = Role.AGENT,
    **overrides: Any,
) -> User:
    await set_tenant_guc(session, tenant_id)
    user = User(
        tenant_id=tenant_id,
        password_hash=password_hash,
        **UserFactory(role=role, **overrides),
    )
    session.add(user)
    await session.flush()
    return user


async def insert_listing(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
    **overrides: Any,
) -> Listing:
    await set_tenant_guc(session, tenant_id)
    listing = Listing(tenant_id=tenant_id, agent_id=agent_id, **ListingFactory(**overrides))
    session.add(listing)
    await session.flush()
    return listing


async def insert_contact(session: AsyncSession, tenant_id: uuid.UUID, **overrides: Any) -> Contact:
    await set_tenant_guc(session, tenant_id)
    contact = Contact(tenant_id=tenant_id, **ContactFactory(**overrides))
    session.add(contact)
    await session.flush()
    return contact


async def insert_lead(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    contact_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    **overrides: Any,
) -> Lead:
    """A lead, minting the mandatory contact when the caller has no opinion."""
    await set_tenant_guc(session, tenant_id)
    if contact_id is None:
        contact_id = (await insert_contact(session, tenant_id)).id
    lead = Lead(
        tenant_id=tenant_id, contact_id=contact_id, agent_id=agent_id, **LeadFactory(**overrides)
    )
    session.add(lead)
    await session.flush()
    return lead


async def insert_deal(
    session: AsyncSession, tenant_id: uuid.UUID, *, owner_user_id: uuid.UUID, **overrides: Any
) -> Deal:
    await set_tenant_guc(session, tenant_id)
    deal = Deal(tenant_id=tenant_id, owner_user_id=owner_user_id, **DealFactory(**overrides))
    session.add(deal)
    await session.flush()
    return deal
