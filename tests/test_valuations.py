"""Valuations (§8.8): the multi-step seller lead magnet and the mortgage
calculator.

The valuation flow — start (address) → details → complete (contact) — is held
together by an HMAC capability token; completion computes an interquartile
price/m² band over nearby published/sold sale comps, mints a ``valuation``
lead, and drops the property payload + estimate on the lead timeline. The
mortgage calculator is stateless Decimal amortization; its "email me" variant
is a capture-defended ``mortgage`` lead source.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from httpx import AsyncClient

from app.core.permissions import Role
from tests.helpers import HOST_A
from tests.test_leads import PORTAL_LEADS, mailpit_count
from tests.test_listings import make_listing, tenant_and_login, transition

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

VALUATIONS = "/api/v1/valuations"
MORTGAGE = "/api/v1/tools/mortgage-estimate"

# The subject property's map-pin — every comp is seeded a few metres away so
# the tightest radius rung (2 km) already reaches MIN_COMPS.
SUBJECT_LAT = 36.7525
SUBJECT_LNG = 3.042


def rendered_at(seconds_ago: int = 30) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


async def start(client: AsyncClient, **overrides: Any) -> httpx.Response:
    body: dict[str, Any] = {
        "city": "Alger",
        "lat": SUBJECT_LAT,
        "lng": SUBJECT_LNG,
        **overrides,
    }
    return await client.post(VALUATIONS, json=body, headers={"Host": HOST_A})


async def set_details(client: AsyncClient, token: str, **fields: Any) -> httpx.Response:
    return await client.patch(
        f"{VALUATIONS}/{token}", json=fields, headers={"Host": HOST_A}
    )


async def complete(client: AsyncClient, token: str, **overrides: Any) -> httpx.Response:
    body: dict[str, Any] = {
        "contact": {"firstName": "Seller", "email": "seller@example.com"},
        "renderedAt": rendered_at(),
        **overrides,
    }
    return await client.post(
        f"{VALUATIONS}/{token}/complete", json=body, headers={"Host": HOST_A}
    )


async def seed_comps(
    client: AsyncClient, admin: dict[str, str], *, prices: list[str], area: str = "100.00"
) -> None:
    """Publish sale listings at the subject point so they land in the comp
    set. Each is nudged a hair north so distinct listings share no exact
    coordinate (irrelevant to the metric cut, tidy for realism)."""
    for i, price in enumerate(prices):
        listing = await make_listing(
            client,
            admin,
            price=price,
            areaBuilt=area,
            propertyType="apartment",
            purpose="sale",
            location={"lat": SUBJECT_LAT + i * 0.0001, "lng": SUBJECT_LNG},
        )
        assert (
            await transition(client, admin, listing["id"], "published")
        ).status_code == 200


async def lead_activities(
    client: AsyncClient, admin: dict[str, str], lead_id: str
) -> list[dict[str, Any]]:
    resp = await client.get(f"{PORTAL_LEADS}/{lead_id}/activities", headers=admin)
    assert resp.status_code == 200, resp.text
    return list(resp.json())


# ---- the valuation step flow ----


async def test_full_flow_estimates_and_mints_lead(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    # 5 comps at 1M each on 100 m² → price/m² = 10000; subject is 80 m²
    # → band collapses to 800000/800000.
    await seed_comps(client, admin, prices=["1000000.00"] * 5, area="100.00")

    started = await start(client)
    assert started.status_code == 201, started.text
    token = started.json()["token"]

    detailed = await set_details(
        client, token, propertyType="apartment", areaBuilt="80.00", beds=2, condition="good"
    )
    assert detailed.status_code == 200, detailed.text
    assert detailed.json()["areaBuilt"] == "80.00"
    assert detailed.json()["details"]["condition"] == "good"

    done = await complete(client, token)
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["compsCount"] == 5
    assert body["estimateLow"] == "800000.00"
    assert body["estimateHigh"] == "800000.00"
    assert body["currency"] == "DZD"
    assert "not an appraisal" in body["disclaimer"]

    # The lead landed with source=valuation and the property on its timeline.
    items = (await client.get(PORTAL_LEADS, headers=admin)).json()["items"]
    lead = next(x for x in items if x["source"] == "valuation")
    activities = await lead_activities(client, admin, lead["id"])
    system = next(a for a in activities if a["payload"].get("kind") == "valuation_request")
    assert system["payload"]["area_built"] == "80.00"
    assert system["payload"]["property_type"] == "apartment"
    assert system["payload"]["estimate_low"] == "800000.00"
    assert system["payload"]["condition"] == "good"


async def test_partial_abandon_leaves_no_lead(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    started = await start(client)
    token = started.json()["token"]
    # Give step 2 but never complete — the row exists, no lead does.
    assert (await set_details(client, token, areaBuilt="90.00")).status_code == 200
    assert (await client.get(PORTAL_LEADS, headers=admin)).json()["items"] == []


async def test_too_few_comps_yields_null_band_but_real_lead(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    # Only 2 comps — below MIN_COMPS on every rung.
    await seed_comps(client, admin, prices=["1000000.00", "1100000.00"])

    token = (await start(client)).json()["token"]
    await set_details(client, token, propertyType="apartment", areaBuilt="80.00")
    done = await complete(client, token)
    assert done.status_code == 200, done.text
    assert done.json()["estimateLow"] is None
    assert done.json()["estimateHigh"] is None
    assert done.json()["compsCount"] == 0

    # The lead is still created — an agent picks up where the algorithm can't.
    items = (await client.get(PORTAL_LEADS, headers=admin)).json()["items"]
    assert any(x["source"] == "valuation" for x in items)


async def test_no_point_yields_null_band(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await seed_comps(client, admin, prices=["1000000.00"] * 5)

    # No lat/lng — comps can't be found without a subject point.
    started = await start(client, lat=None, lng=None)
    token = started.json()["token"]
    await set_details(client, token, propertyType="apartment", areaBuilt="80.00")
    done = await complete(client, token)
    assert done.status_code == 200, done.text
    assert done.json()["estimateLow"] is None


async def test_forged_and_foreign_tenant_tokens_404(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    token = (await start(client)).json()["token"]

    # Tampered signature.
    forged = await set_details(client, token + "x", areaBuilt="80.00")
    assert forged.status_code == 404, forged.text
    # Not-a-token.
    assert (await set_details(client, "garbage.sig", areaBuilt="80.00")).status_code == 404

    # A second tenant on HOST_B can't drive tenant A's request.
    tenant_b = await client.post(
        "/api/v1/platform/tenants",
        json={"name": "Agency B", "slug": "agency-b", "domain": "agency-b.test"},
        headers=platform_headers,
    )
    assert tenant_b.status_code == 201, tenant_b.text
    cross = await client.patch(
        f"{VALUATIONS}/{token}", json={"areaBuilt": "80.00"}, headers={"Host": "agency-b.test"}
    )
    assert cross.status_code == 404, cross.text


async def test_double_complete_conflicts(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    token = (await start(client)).json()["token"]
    assert (await complete(client, token)).status_code == 200
    # The token is spent — a second complete is a 409, a later patch a 404.
    assert (await complete(client, token)).status_code == 409
    assert (await set_details(client, token, areaBuilt="80.00")).status_code == 404


async def test_complete_honeypot_persists_nothing(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await seed_comps(client, admin, prices=["1000000.00"] * 5)
    token = (await start(client)).json()["token"]
    await set_details(client, token, propertyType="apartment", areaBuilt="80.00")

    done = await complete(client, token, hp="gotcha")
    # A bot sees a normal null-band response...
    assert done.status_code == 200, done.text
    assert done.json()["estimateLow"] is None
    # ...but no lead, and the token is still live (nothing was committed).
    assert (await client.get(PORTAL_LEADS, headers=admin)).json()["items"] == []
    assert (await complete(client, token)).status_code == 200


# ---- mortgage calculator ----


async def mortgage(client: AsyncClient, **body: Any) -> httpx.Response:
    return await client.post(MORTGAGE, json=body, headers={"Host": HOST_A})


async def test_mortgage_math_against_known_values(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    # 100000 loan (price 120000, down 20000), 6% / 30y → 599.55/mo (standard
    # amortization reference value).
    resp = await mortgage(
        client,
        price="120000.00",
        downPayment="20000.00",
        annualRatePercent="6",
        termYears=30,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["loanAmount"] == "100000.00"
    assert body["monthlyPayment"] == "599.55"
    # 599.55 * 360 = 215838.00; interest = 115838.00.
    assert body["totalPaid"] == "215838.00"
    assert body["totalInterest"] == "115838.00"


async def test_mortgage_zero_rate_is_straight_division(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await mortgage(
        client, price="120000.00", downPayment="0", annualRatePercent="0", termYears=10
    )
    assert resp.status_code == 200, resp.text
    # 120000 / 120 months = 1000.00, no interest.
    assert resp.json()["monthlyPayment"] == "1000.00"
    assert resp.json()["totalInterest"] == "0.00"


async def test_mortgage_uses_tenant_default_rates(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(
        client,
        platform_headers,
        create_tenant_user,
        Role.ADMIN,
        settings={
            "mortgage": {
                "default_annual_rate_percent": 0,
                "default_term_years": 10,
                "default_down_payment_percent": 25,
            }
        },
    )
    # Omit every optional field → tenant defaults apply: 25% down on 120000 =
    # 30000, 90000 over 120 months at 0% = 750.00/mo.
    resp = await mortgage(client, price="120000.00")
    assert resp.status_code == 200, resp.text
    assert resp.json()["downPayment"] == "30000.00"
    assert resp.json()["annualRatePercent"] == "0"
    assert resp.json()["termYears"] == 10
    assert resp.json()["monthlyPayment"] == "750.00"


async def test_mortgage_rejects_down_payment_above_price(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await mortgage(client, price="100000.00", downPayment="100000.00")
    assert resp.status_code == 422, resp.text


async def test_mortgage_email_creates_lead_and_sends(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    to = "mortgage-buyer@example.com"
    before = await mailpit_count(to, "mortgage")

    resp = await client.post(
        f"{MORTGAGE}/email",
        json={
            "price": "120000.00",
            "downPayment": "20000.00",
            "annualRatePercent": "6",
            "termYears": 30,
            "contact": {"firstName": "Buyer", "email": to},
            "renderedAt": rendered_at(),
        },
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["estimate"]["monthlyPayment"] == "599.55"

    items = (await client.get(PORTAL_LEADS, headers=admin)).json()["items"]
    lead = next(x for x in items if x["source"] == "mortgage")
    activities = await lead_activities(client, admin, lead["id"])
    assert any(a["payload"].get("kind") == "mortgage_estimate" for a in activities)

    after = await mailpit_count(to, "mortgage")
    assert after == before + 1


async def test_mortgage_email_honeypot_persists_nothing(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.post(
        f"{MORTGAGE}/email",
        json={
            "price": "120000.00",
            "contact": {"firstName": "Bot", "email": "bot@example.com"},
            "renderedAt": rendered_at(),
            "hp": "gotcha",
        },
        headers={"Host": HOST_A},
    )
    # Still returns the estimate — a bot gets nothing to distinguish...
    assert resp.status_code == 201, resp.text
    assert Decimal(resp.json()["estimate"]["monthlyPayment"]) > 0
    # ...but no lead was created.
    assert (await client.get(PORTAL_LEADS, headers=admin)).json()["items"] == []


async def test_mortgage_email_honeypot_without_email_still_camouflaged(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """A bot filling hp but omitting email must get the same 201 as any other
    honeypot hit — never a distinguishable 422 that unmasks the honeypot."""
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.post(
        f"{MORTGAGE}/email",
        json={
            "price": "120000.00",
            "contact": {"phone": "+213770000000"},  # phone-only, no email
            "renderedAt": rendered_at(),
            "hp": "gotcha",
        },
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 201, resp.text
    assert (await client.get(PORTAL_LEADS, headers=admin)).json()["items"] == []


async def test_mortgage_email_requires_email_for_real_submission(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """A genuine submission (no hp) still needs an email to mail to — 422."""
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.post(
        f"{MORTGAGE}/email",
        json={
            "price": "120000.00",
            "contact": {"phone": "+213770000000"},  # phone-only, no email
            "renderedAt": rendered_at(),
        },
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 422, resp.text


async def test_mortgage_email_rejects_unknown_listing(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """A client-supplied listingId that isn't a real published listing is a
    404, not a 500 from an FK violation on the lead insert."""
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.post(
        f"{MORTGAGE}/email",
        json={
            "price": "120000.00",
            "listingId": "00000000-0000-0000-0000-000000000000",
            "contact": {"firstName": "Buyer", "email": "listing-buyer@example.com"},
            "renderedAt": rendered_at(),
        },
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 404, resp.text
    assert (await client.get(PORTAL_LEADS, headers=admin)).json()["items"] == []
