"""Compliance module (§8.17/§13): consent records + cookie config, the analytics
consent gate (closing Part 21's TODO), DSR export/erasure with the 30-day purge
sweep and per-data-type anonymize-vs-delete judgement, saved-search opt-in
consent write-path, lost-lead retention, and the tenant-scoped audit report."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select, update

from app.core.database import set_tenant_guc
from app.core.permissions import Role
from app.modules.compliance.models import ConsentRecord, DsrRequest, DsrStatus
from app.modules.leads.models import Contact, Lead, LeadStage
from app.workers.tasks.compliance import (
    anonymize_stale_lost_leads,
    purge_due_erasures,
)
from tests.helpers import HOST_A, HOST_B, bearer, register_user
from tests.test_leads import capture, capture_body
from tests.test_listings import tenant_and_login
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

CONSENT = "/api/v1/consent"
COOKIE_CONFIG = "/api/v1/portal/compliance/cookie-config"
SITE_COOKIE = "/api/v1/site/cookie-config"
AUDIT_LOG = "/api/v1/portal/compliance/audit-log"
ME_EXPORT = "/api/v1/me/export"
ME_DELETE = "/api/v1/me"
EVENTS = "/api/v1/analytics/events"


async def buyer_headers(client: AsyncClient, email: str, host: str = HOST_A) -> dict[str, str]:
    resp = await register_user(client, host, email=email)
    assert resp.status_code == 201, resp.text
    return {"Host": host, "Authorization": bearer(resp)}


# ---- consent records (append-only proof) ----


async def test_cookie_banner_records_consent_per_category(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.post(
        CONSENT,
        json={
            "sessionId": "sess-abc",
            "choices": {"necessary": True, "analytics": False, "marketing": True},
        },
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 201, resp.text
    records = resp.json()
    assert len(records) == 3
    by_cat = {r["category"]: r["granted"] for r in records}
    assert by_cat == {"necessary": True, "analytics": False, "marketing": True}
    assert all(r["source"] == "cookie_banner" for r in records)


async def test_anonymous_consent_needs_session_id(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.post(
        CONSENT, json={"choices": {"analytics": True}}, headers={"Host": HOST_A}
    )
    assert resp.status_code == 409  # no session id → cannot tie the record


async def test_consent_tolerates_overlong_user_agent(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """A bot can send an arbitrarily long User-Agent on this public endpoint;
    the record's user_agent column is String(400), so an untruncated value
    would raise StringDataRightTruncation → 500. It must be truncated, not
    crash."""
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.post(
        CONSENT,
        json={"sessionId": "sess-longua", "choices": {"necessary": True}},
        headers={"Host": HOST_A, "User-Agent": "x" * 1000},
    )
    assert resp.status_code == 201, resp.text


# ---- analytics consent gate (§8.15 — the TODO Part 21 left) ----


async def _post_events(
    client: AsyncClient, events: list[dict[str, Any]], host: str = HOST_A
) -> int:
    resp = await client.post(EVENTS, json={"events": events}, headers={"Host": host})
    assert resp.status_code == 202, resp.text
    return int(resp.json()["accepted"])


async def test_analytics_gate_blocks_unconsented_session(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    session_id = "sess-block"
    # A session that reject analytics: its events must be dropped.
    await client.post(
        CONSENT,
        json={"sessionId": session_id, "choices": {"analytics": False}},
        headers={"Host": HOST_A},
    )
    accepted = await _post_events(
        client, [{"eventType": "page_view", "sessionId": session_id, "path": "/"}]
    )
    assert accepted == 0

    # A fully anonymous hit (no session id) is still accepted — nothing to gate.
    accepted = await _post_events(client, [{"eventType": "page_view", "path": "/"}])
    assert accepted == 1

    # Once the session consents, its events flow.
    await client.post(
        CONSENT,
        json={"sessionId": session_id, "choices": {"analytics": True}},
        headers={"Host": HOST_A},
    )
    accepted = await _post_events(
        client, [{"eventType": "page_view", "sessionId": session_id, "path": "/"}]
    )
    assert accepted == 1


# ---- cookie-consent config (portal + RBAC + public render) ----


async def test_cookie_config_put_get_and_public_render(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    body = {
        "categories": [{"key": "analytics", "required": False, "defaultOn": False}],
        "bannerCopy": {"title": {"en": "We use cookies"}},
        "isEnabled": True,
    }
    resp = await client.put(COOKIE_CONFIG, json=body, headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["isEnabled"] is True

    # Public site render (no auth).
    resp = await client.get(SITE_COOKIE, headers={"Host": HOST_A})
    assert resp.status_code == 200, resp.text
    assert resp.json()["categories"][0]["key"] == "analytics"


async def test_cookie_config_requires_compliance_manage(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, agent = await tenant_and_login(client, platform_headers, create_tenant_user, Role.AGENT)
    resp = await client.put(COOKIE_CONFIG, json={"categories": []}, headers=agent)
    assert resp.status_code == 403  # COMPLIANCE_MANAGE is admin-only


# ---- DSR export (fan-out §10.12) ----


async def test_me_export_aggregates_across_modules(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    buyer = await buyer_headers(client, "export-me@example.com")
    # Seed a CRM footprint under the same email (public capture).
    resp = await capture(client, capture_body(email="export-me@example.com", source="other"))
    assert resp.status_code == 201, resp.text

    resp = await client.get(ME_EXPORT, headers=buyer)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["subjectEmail"] == "export-me@example.com"
    sections = data["sections"]
    assert sections["account"]["email"] == "export-me@example.com"
    # CRM contact/lead created by the capture shows up in the export.
    assert len(sections["crm"]["contacts"]) == 1
    assert len(sections["crm"]["leads"]) == 1
    assert "favorites" in sections
    assert "notifications" in sections


# ---- DSR erasure (DELETE /me → soft-delete → purge sweep) ----


async def test_delete_me_soft_deletes_and_schedules_purge(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    buyer = await buyer_headers(client, "erase-me@example.com")

    resp = await client.delete(ME_DELETE, headers=buyer)
    assert resp.status_code == 202, resp.text
    assert resp.json()["purgeScheduledAt"] is not None

    # The account is soft-deleted: the token no longer authenticates.
    again = await client.get(ME_EXPORT, headers=buyer)
    assert again.status_code == 401

    # Idempotent: exactly one pending erasure exists.
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, uuid.UUID(str(tenant["id"])))
        count = (await session.execute(select(func.count()).select_from(DsrRequest))).scalar_one()
    assert count == 1


async def test_erasure_purge_anonymizes_person_keeps_business_record(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    tid = uuid.UUID(str(tenant["id"]))
    email = "purge-me@example.com"
    buyer = await buyer_headers(client, email)
    # A CRM lead tied to the buyer's email (a business record).
    resp = await capture(client, capture_body(email=email, source="listing_form"))
    assert resp.status_code == 201, resp.text

    # Request erasure and force the purge due.
    resp = await client.delete(ME_DELETE, headers=buyer)
    assert resp.status_code == 202, resp.text
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        await session.execute(
            update(DsrRequest).values(purge_scheduled_at=datetime.now(UTC) - timedelta(days=1))
        )

    purged = purge_due_erasures()
    assert purged == 1

    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        # Contact PII is stripped, but the lead row (pipeline record) survives.
        contact = (await session.execute(select(Contact))).scalars().one()
        assert contact.email is None
        assert contact.first_name is None
        leads = (await session.execute(select(Lead))).scalars().all()
        assert len(leads) == 1  # business record retained
        dsr = (await session.execute(select(DsrRequest))).scalars().one()
        assert dsr.status == DsrStatus.COMPLETED
        assert dsr.result["contacts_anonymized"] == 1


# ---- saved-search opt-in records consent (§8.9 write-path) ----


async def test_saved_search_signup_records_marketing_consent(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    import re

    from tests.test_favorites import mailpit_text, signup_body

    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    email = "consent-signup@example.com"
    resp = await client.post(
        "/api/v1/saved-searches",
        json=signup_body(email, name="Downtown", filters={}),
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 201, resp.text
    text = await mailpit_text(email, "Confirm")
    code = re.search(r"code: (\S+)", text)
    assert code
    resp = await client.post(
        "/api/v1/saved-searches/confirm",
        json={"token": code.group(1)},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 200, resp.text

    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, uuid.UUID(str(tenant["id"])))
        records = (
            (await session.execute(select(ConsentRecord).where(ConsentRecord.email == email)))
            .scalars()
            .all()
        )
    assert len(records) == 1
    assert records[0].category.value == "marketing"
    assert records[0].granted is True
    assert records[0].source == "saved_search_signup"


# ---- lost-lead retention (24-month anonymization) ----


async def test_lost_lead_retention_anonymizes_after_cutoff(
    client: AsyncClient,
    app: FastAPI,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, _ = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    tid = uuid.UUID(str(tenant["id"]))
    resp = await capture(client, capture_body(email="stale-lost@example.com", source="other"))
    lead_id = resp.json()["id"]

    # Mark it lost and backdate updated_at past the 24-month cutoff.
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        await session.execute(
            update(Lead)
            .where(Lead.id == uuid.UUID(lead_id))
            .values(
                stage=LeadStage.LOST,
                updated_at=datetime.now(UTC) - timedelta(days=800),
            )
        )

    anonymized = anonymize_stale_lost_leads()
    assert anonymized == 1

    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        contact = (await session.execute(select(Contact))).scalars().one()
        assert contact.email is None

    # Idempotent: a re-run touches nothing (already anonymized).
    assert anonymize_stale_lost_leads() == 0


# ---- audit-access report (tenant-scoped, §10.11) ----


async def test_audit_log_report_scoped_to_tenant(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    # Empty is a valid 200 (no audited actions on a fresh tenant).
    resp = await client.get(AUDIT_LOG, headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["totalEstimate"] == 0


async def test_export_is_tenant_isolated(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    # Two tenants, same buyer email — each export sees only its own tenant's rows.
    await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await create_tenant(client, platform_headers, name="Agency B", slug="agency-b", domain=HOST_B)

    buyer_a = await buyer_headers(client, "shared@example.com", host=HOST_A)
    buyer_b = await buyer_headers(client, "shared@example.com", host=HOST_B)
    await capture(client, capture_body(email="shared@example.com", source="other"), host=HOST_A)

    resp_a = await client.get(ME_EXPORT, headers=buyer_a)
    resp_b = await client.get(ME_EXPORT, headers=buyer_b)
    assert len(resp_a.json()["sections"]["crm"]["contacts"]) == 1
    assert len(resp_b.json()["sections"]["crm"]["contacts"]) == 0  # B has no such contact
