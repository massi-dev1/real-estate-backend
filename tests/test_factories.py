"""Exercises the factories (§13 factory_boy) and the RLS floor beneath them.

Two jobs. First, a factory nobody runs is dead code that rots silently, so
these tests keep ``tests/factories.py`` honest — every ``insert_*`` helper is
called and its row read back. Second, they assert isolation one layer *below*
``test_tenant_isolation.py``: that harness proves the API returns 404 across
tenants, which is the behaviour users see, but the reason it holds is Postgres
RLS refusing the row in the first place. Testing the database directly means a
service that forgot its ``tenant_id`` filter still cannot leak, and it is the
"many service/repository tests against real Postgres" layer §13's pyramid puts
at the base.
"""

import uuid

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.core.database import set_tenant_guc
from app.core.permissions import Role
from app.modules.leads.models import Lead
from app.modules.listings.models import Listing, ListingStatus
from app.modules.transactions.models import Deal
from tests.conftest import _FIXTURE_PASSWORD_HASH
from tests.factories import (
    ListingFactory,
    TenantFactory,
    insert_contact,
    insert_deal,
    insert_lead,
    insert_listing,
    insert_tenant,
    insert_user,
)


async def test_factories_produce_unique_values() -> None:
    """Sequences, not constants: two builds must not collide on a unique
    column, or a test that makes two of anything fails on a constraint rather
    than on the thing it meant to assert."""
    first, second = TenantFactory(), TenantFactory()
    assert first["slug"] != second["slug"]
    assert ListingFactory()["reference_code"] != ListingFactory()["reference_code"]


async def test_overrides_beat_factory_defaults() -> None:
    built = TenantFactory(name="Specific Agency", plan="pro")
    assert built["name"] == "Specific Agency"
    assert built["plan"] == "pro"
    # Untouched fields still come from the factory.
    assert built["slug"].startswith("agency-")


async def test_insert_helpers_round_trip_every_entity(app: FastAPI) -> None:
    """One row per factory, read back through the ORM."""
    async with app.state.session_factory() as session, session.begin():
        tenant = await insert_tenant(session, domain="factories-a.test")
        agent = await insert_user(
            session, tenant.id, password_hash=_FIXTURE_PASSWORD_HASH, role=Role.AGENT
        )
        listing = await insert_listing(
            session, tenant.id, agent_id=agent.id, status=ListingStatus.PUBLISHED
        )
        contact = await insert_contact(session, tenant.id)
        lead = await insert_lead(session, tenant.id, contact_id=contact.id, agent_id=agent.id)
        deal = await insert_deal(session, tenant.id, owner_user_id=agent.id)

        assert listing.tenant_id == tenant.id
        assert listing.status is ListingStatus.PUBLISHED  # override applied
        assert lead.contact_id == contact.id
        assert deal.owner_user_id == agent.id


async def test_insert_lead_mints_its_own_contact(app: FastAPI) -> None:
    """A lead's ``contact_id`` is mandatory (§8.4), so the helper provides one
    when the caller does not care which contact it is."""
    async with app.state.session_factory() as session, session.begin():
        tenant = await insert_tenant(session, domain="factories-b.test")
        lead = await insert_lead(session, tenant.id)
        assert lead.contact_id is not None


@pytest.mark.parametrize(
    ("model", "label"),
    [(Listing, "listing"), (Lead, "lead"), (Deal, "deal")],
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
async def test_rls_hides_another_tenants_rows(app: FastAPI, model: type, label: str) -> None:
    """The database, not the query, is what stops the leak.

    Rows are written for tenant A, then counted from a session scoped to
    tenant B with **no tenant filter in the query at all**. RLS is fail-closed,
    so the count must be zero; if it were not, every ``WHERE tenant_id = ...``
    in the codebase would be the only thing standing between two agencies.
    """
    async with app.state.session_factory() as session, session.begin():
        tenant_a = await insert_tenant(session, domain=f"rls-a-{label}.test")
        tenant_b = await insert_tenant(session, domain=f"rls-b-{label}.test")
        agent_a = await insert_user(
            session, tenant_a.id, password_hash=_FIXTURE_PASSWORD_HASH, role=Role.AGENT
        )
        if model is Listing:
            await insert_listing(session, tenant_a.id, agent_id=agent_a.id)
        elif model is Lead:
            await insert_lead(session, tenant_a.id, agent_id=agent_a.id)
        else:
            await insert_deal(session, tenant_a.id, owner_user_id=agent_a.id)
        a_id, b_id = tenant_a.id, tenant_b.id

    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, a_id)
        own = await session.scalar(select(func.count()).select_from(model))
        assert own == 1, f"{label}: tenant A cannot see its own row"

    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, b_id)
        foreign = await session.scalar(select(func.count()).select_from(model))
        assert foreign == 0, f"{label}: tenant B counted {foreign} of tenant A's rows"


async def test_rls_refuses_an_unscoped_read(app: FastAPI) -> None:
    """With no ``app.tenant_id`` set at all, a tenant table **errors**.

    The policy reads ``current_setting('app.tenant_id')`` without
    ``missing_ok`` on purpose (``core/rls.py``): fail closed, *loudly*. That
    is stronger than returning zero rows — a silent empty result reads like
    "this tenant has no listings" and a developer ships the bug, whereas an
    error is impossible to mistake for data. Asserting it here pins the
    distinction so a future migration cannot quietly relax it.
    """
    async with app.state.session_factory() as session, session.begin():
        tenant = await insert_tenant(session, domain="rls-unscoped.test")
        agent = await insert_user(
            session, tenant.id, password_hash=_FIXTURE_PASSWORD_HASH, role=Role.AGENT
        )
        await insert_listing(session, tenant.id, agent_id=agent.id)

    with pytest.raises(DBAPIError) as caught:
        async with app.state.session_factory() as session, session.begin():
            await session.scalar(select(func.count()).select_from(Listing))
    # The unset GUC reaches Postgres as an empty string, which fails the
    # ::uuid cast in the policy — never as an unfiltered read.
    assert "uuid" in str(caught.value).lower()


async def test_rls_blocks_writing_into_another_tenant(app: FastAPI) -> None:
    """A row addressed to tenant A while scoped to tenant B must not land —
    the policy covers INSERT, not just SELECT, so a confused-deputy write is
    refused at the database."""
    async with app.state.session_factory() as session, session.begin():
        tenant_a = await insert_tenant(session, domain="rls-write-a.test")
        tenant_b = await insert_tenant(session, domain="rls-write-b.test")
        a_id, b_id = tenant_a.id, tenant_b.id

    with pytest.raises(DBAPIError) as caught:
        async with app.state.session_factory() as session, session.begin():
            await set_tenant_guc(session, b_id)
            # tenant_id says A, but the session is scoped to B.
            session.add(Listing(tenant_id=a_id, agent_id=None, **ListingFactory()))
            await session.flush()
    assert "policy" in str(caught.value).lower() or "violates" in str(caught.value).lower()


async def test_unknown_tenant_scope_sees_nothing(app: FastAPI) -> None:
    """A GUC naming a tenant that does not exist is not an error — it simply
    matches no rows, which keeps a stale/forged context harmless."""
    async with app.state.session_factory() as session, session.begin():
        tenant = await insert_tenant(session, domain="rls-ghost.test")
        agent = await insert_user(
            session, tenant.id, password_hash=_FIXTURE_PASSWORD_HASH, role=Role.AGENT
        )
        await insert_listing(session, tenant.id, agent_id=agent.id)

    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, uuid.uuid4())
        assert await session.scalar(select(func.count()).select_from(Listing)) == 0
