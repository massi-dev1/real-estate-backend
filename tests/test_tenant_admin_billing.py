"""Part 22 (§8.16): plan quotas, tenant lifecycle (trial/offboard), domain DNS
verification, billing (checkout/webhook/dunning), platform metrics, and
audit-logged impersonation."""

import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text

from app.core.permissions import Role
from app.integrations.billing.stub import StubBillingProvider
from app.modules.tenants.plans import PLANS
from tests.helpers import HOST_A
from tests.test_listings import LISTING_BODY, tenant_and_login
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]


# ---- plan quotas (§8.16) ----


async def _set_plan(
    client: AsyncClient, platform_headers: dict[str, str], tenant_id: str, plan: str
) -> None:
    resp = await client.put(
        f"/api/v1/platform/tenants/{tenant_id}/plan",
        json={"plan": plan},
        headers=platform_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["plan"] == plan


async def test_new_tenant_starts_on_trial_with_end_date(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    body = await create_tenant(client, platform_headers)
    assert body["status"] == "trial"
    assert body["plan"] == "trial"
    assert body["trialEndsAt"] is not None
    # Primary domain gets a verification challenge.
    assert body["domains"][0]["verificationStatus"] == "pending"
    assert body["domains"][0]["verificationToken"]


async def test_unknown_plan_rejected(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    body = await create_tenant(client, platform_headers)
    resp = await client.put(
        f"/api/v1/platform/tenants/{body['id']}/plan",
        json={"plan": "does-not-exist"},
        headers=platform_headers,
    )
    assert resp.status_code == 409


async def test_listing_quota_enforced_and_released(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    # Shrink the plan to a 2-listing ceiling via a bespoke plan monkeypatch is
    # awkward; instead use the real trial plan (25) but drive to the edge by
    # patching the trial limit down for this test.
    original = PLANS["trial"]
    PLANS["trial"] = original.__class__(
        key="trial",
        name="Trial",
        max_listings=2,
        max_agents=original.max_agents,
        storage_gb=original.storage_gb,
        monthly_emails=original.monthly_emails,
    )
    try:
        created_ids = []
        for _ in range(2):
            resp = await client.post(
                "/api/v1/portal/listings", json=LISTING_BODY, headers=admin
            )
            assert resp.status_code == 201, resp.text
            created_ids.append(resp.json()["id"])
        # Third exceeds the quota → 403 quota-exceeded problem+json.
        resp = await client.post(
            "/api/v1/portal/listings", json=LISTING_BODY, headers=admin
        )
        assert resp.status_code == 403
        assert resp.json()["type"].endswith("quota-exceeded")

        # Deleting one frees a slot (archive first — published-workflow states
        # are undeletable, but a fresh draft deletes straight away).
        resp = await client.delete(
            f"/api/v1/portal/listings/{created_ids[0]}", headers=admin
        )
        assert resp.status_code in (200, 204), resp.text
        resp = await client.post(
            "/api/v1/portal/listings", json=LISTING_BODY, headers=admin
        )
        assert resp.status_code == 201, resp.text
    finally:
        PLANS["trial"] = original


async def test_site_config_surfaces_usage_and_limits(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    resp = await client.post("/api/v1/portal/listings", json=LISTING_BODY, headers=admin)
    assert resp.status_code == 201

    resp = await client.get("/api/v1/site/config", headers={"Host": HOST_A})
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"] == "trial"
    assert body["usage"]["listingsCount"] == 1
    assert body["limits"]["maxListings"] == PLANS["trial"].max_listings


# ---- domain verification (§8.16) ----


async def test_domain_verification_pass_and_fail(
    client: AsyncClient, platform_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    body = await create_tenant(client, platform_headers)
    tenant_id = body["id"]
    domain = body["domains"][0]
    token = domain["verificationToken"]

    # Stub the DNS resolver: first a domain that fails, then one that passes.
    from app.modules.tenants import service as tenant_service

    async def _empty_resolver(_domain: str) -> list[str]:
        return []

    monkeypatch.setattr(tenant_service, "default_txt_lookup", _empty_resolver)
    resp = await client.post(
        f"/api/v1/platform/tenants/{tenant_id}/domains/{domain['id']}/verify",
        headers=platform_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["verificationStatus"] == "failed"

    async def _match_resolver(_domain: str) -> list[str]:
        return [token]

    monkeypatch.setattr(tenant_service, "default_txt_lookup", _match_resolver)
    resp = await client.post(
        f"/api/v1/platform/tenants/{tenant_id}/domains/{domain['id']}/verify",
        headers=platform_headers,
    )
    assert resp.status_code == 200
    verified = resp.json()
    assert verified["verificationStatus"] == "verified"
    assert verified["verifiedAt"] is not None


# ---- offboarding (§8.16) ----


async def test_offboard_suspends_exports_and_schedules_deletion(
    app: FastAPI,
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    tenant_id = tenant["id"]
    # Give the tenant a listing so the export has content.
    await client.post("/api/v1/portal/listings", json=LISTING_BODY, headers=admin)

    resp = await client.post(
        f"/api/v1/platform/tenants/{tenant_id}/offboard", headers=platform_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "suspended"
    assert body["offboardingAt"] is not None
    assert body["deletionScheduledAt"] is not None

    # The export task ran eagerly (post-commit) and stamped an object key.
    async with app.state.engine.begin() as conn:
        key = (
            await conn.execute(
                text("SELECT export_object_key FROM tenants WHERE id = :id"),
                {"id": tenant_id},
            )
        ).scalar_one()
    assert key is not None

    # A suspended tenant's site is 402.
    resp = await client.get("/api/v1/site/config", headers={"Host": HOST_A})
    assert resp.status_code == 402

    # Cancel-offboard reactivates.
    resp = await client.post(
        f"/api/v1/platform/tenants/{tenant_id}/offboard/cancel", headers=platform_headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


async def test_purge_scheduled_tenant_deletes_it(
    app: FastAPI,
    client: AsyncClient,
    platform_headers: dict[str, str],
) -> None:
    body = await create_tenant(client, platform_headers)
    tenant_id = body["id"]
    # Move the deletion instant into the past so the purge sweep picks it up.
    async with app.state.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE tenants SET deletion_scheduled_at = :past WHERE id = :id"
            ),
            {"past": datetime.now(UTC) - timedelta(days=1), "id": tenant_id},
        )
    from app.workers.tasks.tenants import purge_scheduled_tenants

    purged = purge_scheduled_tenants()
    assert purged >= 1
    resp = await client.get(
        f"/api/v1/platform/tenants/{tenant_id}", headers=platform_headers
    )
    assert resp.status_code == 404


# ---- billing webhooks (§10.9) ----


def _webhook_payload(event_type: str, tenant_id: str, **data: object) -> bytes:
    return json.dumps(
        {
            "id": f"evt_{uuid.uuid4().hex}",
            "type": event_type,
            "data": {"customer_id": tenant_id, **data},
        }
    ).encode()


async def test_webhook_verification_and_lifecycle(
    app: FastAPI, client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    body = await create_tenant(client, platform_headers)
    tenant_id = body["id"]
    provider = StubBillingProvider(app.state.settings.billing_webhook_secret)

    # Activation: valid signature → tenant active, subscription active.
    payload = _webhook_payload(
        "subscription.activated",
        tenant_id,
        subscription_id="sub_123",
        plan="growth",
    )
    resp = await client.post(
        "/api/v1/billing/webhook",
        content=payload,
        headers={"X-Billing-Signature": provider.sign_payload(payload)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"received": True, "processed": True}

    resp = await client.get(
        f"/api/v1/platform/tenants/{tenant_id}", headers=platform_headers
    )
    assert resp.json()["status"] == "active"
    assert resp.json()["plan"] == "growth"

    resp = await client.get(
        f"/api/v1/platform/tenants/{tenant_id}/subscription", headers=platform_headers
    )
    assert resp.json()["status"] == "active"

    # Replay of the *same* event id is idempotent (processed False).
    same_id = json.loads(payload)["id"]
    replay = json.dumps(
        {"id": same_id, "type": "subscription.activated", "data": {"customer_id": tenant_id}}
    ).encode()
    resp = await client.post(
        "/api/v1/billing/webhook",
        content=replay,
        headers={"X-Billing-Signature": provider.sign_payload(replay)},
    )
    assert resp.json() == {"received": True, "processed": False}


async def test_webhook_bad_signature_rejected(
    app: FastAPI, client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    body = await create_tenant(client, platform_headers)
    payload = _webhook_payload("subscription.activated", body["id"], subscription_id="s1")
    resp = await client.post(
        "/api/v1/billing/webhook",
        content=payload,
        headers={"X-Billing-Signature": "t=123,v1=deadbeef"},
    )
    assert resp.status_code == 400
    assert resp.json()["type"].endswith("invalid-webhook")


async def test_webhook_stale_timestamp_rejected(
    app: FastAPI, client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    body = await create_tenant(client, platform_headers)
    provider = StubBillingProvider(app.state.settings.billing_webhook_secret)
    payload = _webhook_payload("subscription.activated", body["id"], subscription_id="s1")
    stale = datetime.now(UTC) - timedelta(minutes=10)
    resp = await client.post(
        "/api/v1/billing/webhook",
        content=payload,
        headers={"X-Billing-Signature": provider.sign_payload(payload, timestamp=stale)},
    )
    assert resp.status_code == 400


async def test_payment_failed_then_dunning_suspends(
    app: FastAPI, client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    body = await create_tenant(client, platform_headers)
    tenant_id = body["id"]
    provider = StubBillingProvider(app.state.settings.billing_webhook_secret)

    # Activate first so a subscription row exists.
    activate = _webhook_payload(
        "subscription.activated", tenant_id, subscription_id="sub_x", plan="starter"
    )
    await client.post(
        "/api/v1/billing/webhook",
        content=activate,
        headers={"X-Billing-Signature": provider.sign_payload(activate)},
    )
    # Payment fails → past_due with a grace window.
    failed = _webhook_payload("payment.failed", tenant_id, subscription_id="sub_x")
    await client.post(
        "/api/v1/billing/webhook",
        content=failed,
        headers={"X-Billing-Signature": provider.sign_payload(failed)},
    )
    resp = await client.get(
        f"/api/v1/platform/tenants/{tenant_id}/subscription", headers=platform_headers
    )
    assert resp.json()["status"] == "past_due"
    # Tenant still reachable during grace.
    resp = await client.get(
        f"/api/v1/platform/tenants/{tenant_id}", headers=platform_headers
    )
    assert resp.json()["status"] == "active"

    # Move the grace deadline into the past and run the dunning sweep.
    async with app.state.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenant_subscriptions SET grace_until = :past WHERE tenant_id = :id"),
            {"past": datetime.now(UTC) - timedelta(hours=1), "id": tenant_id},
        )
    from app.workers.tasks.tenants import run_dunning_sweep

    suspended = run_dunning_sweep()
    assert suspended >= 1
    resp = await client.get(
        f"/api/v1/platform/tenants/{tenant_id}", headers=platform_headers
    )
    assert resp.json()["status"] == "suspended"


async def test_trial_expiry_sweep_suspends(
    app: FastAPI, client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    body = await create_tenant(client, platform_headers)
    tenant_id = body["id"]
    async with app.state.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tenants SET trial_ends_at = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(days=1), "id": tenant_id},
        )
    from app.workers.tasks.tenants import expire_trials

    suspended = expire_trials()
    assert suspended >= 1
    resp = await client.get(
        f"/api/v1/platform/tenants/{tenant_id}", headers=platform_headers
    )
    assert resp.json()["status"] == "suspended"


# ---- platform metrics + impersonation (§8.16/§10.11) ----


async def test_platform_metrics(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    await client.post("/api/v1/portal/listings", json=LISTING_BODY, headers=admin)

    resp = await client.get("/api/v1/platform/metrics", headers=platform_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["totalTenants"] >= 1
    assert body["totalListings"] >= 1
    row = next(r for r in body["tenants"] if r["tenantId"] == tenant["id"])
    assert row["listingsCount"] == 1


async def test_impersonation_mints_token_and_audits(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, _ = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    tenant_id = tenant["id"]

    resp = await client.post(
        f"/api/v1/platform/tenants/{tenant_id}/impersonate", headers=platform_headers
    )
    assert resp.status_code == 200, resp.text
    grant = resp.json()
    assert grant["impersonation"] is True
    assert grant["tenantId"] == tenant_id
    assert grant["accessToken"]

    # The impersonation token works on the tenant's host as the admin.
    imp_headers = {"Host": HOST_A, "Authorization": f"Bearer {grant['accessToken']}"}
    resp = await client.get("/api/v1/portal/listings", headers=imp_headers)
    assert resp.status_code == 200

    # The action is audit-logged.
    resp = await client.get(
        "/api/v1/platform/audit-log?action=tenant.impersonate", headers=platform_headers
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(i["tenantId"] == tenant_id for i in items)
    entry = next(i for i in items if i["tenantId"] == tenant_id)
    assert entry["action"] == "tenant.impersonate"
    assert "tenantSlug" in entry["metadata"] or "tenant_slug" in entry["metadata"]


async def test_impersonation_suspended_tenant_conflict(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, _ = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    await client.post(
        f"/api/v1/platform/tenants/{tenant['id']}/suspend", headers=platform_headers
    )
    resp = await client.post(
        f"/api/v1/platform/tenants/{tenant['id']}/impersonate", headers=platform_headers
    )
    assert resp.status_code == 409


async def test_impersonation_requires_platform_admin(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    # A tenant admin cannot reach the platform impersonation endpoint.
    resp = await client.post(
        f"/api/v1/platform/tenants/{tenant['id']}/impersonate", headers=admin
    )
    assert resp.status_code in (401, 403)
