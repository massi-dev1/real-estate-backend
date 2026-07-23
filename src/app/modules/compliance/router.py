"""HTTP layer for compliance (§8.17).

- ``public_router`` (``/consent``) — the cookie-banner submission: anonymous,
  rate-limited, records the visitor's per-category choices as append-only
  consent proof.
- ``site_router`` (``/site/cookie-config``) — the public banner configuration
  the frontend renders (tenant-resolved, no auth).
- ``me_router`` (``/me/...``) — the buyer/agent DSR surface: ``GET /me/export``
  (data portability) and ``DELETE /me`` (erasure). Ownership is the
  authorization (no RBAC permission), same stance as favorites' ``/me``.
- ``portal_router`` (``/portal/compliance/...``) — tenant-admin config + the
  audit-access report, gated by ``COMPLIANCE_MANAGE``.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.database import SessionDep
from app.core.exceptions import NotFoundError
from app.core.pagination import MAX_PAGE_SIZE, Page, clamp_limit, decode_cursor, encode_cursor
from app.core.permissions import CurrentUserDep, Permission, require
from app.core.rate_limit import rate_limit
from app.core.tenancy import TenantDep
from app.modules.auth.service import AuthServiceDep
from app.modules.compliance.schemas import (
    ConsentIn,
    ConsentRecordOut,
    CookieConsentConfigIn,
    CookieConsentConfigOut,
    DataExportOut,
    DsrRequestOut,
    ErasureAck,
)
from app.modules.compliance.service import ComplianceServiceDep
from app.modules.tenants.audit import AuditRepository
from app.modules.tenants.schemas import AuditLogOut

# ---- public: cookie-banner consent submission ----

public_router = APIRouter(prefix="/consent", tags=["compliance:public"])

_consent_limit = rate_limit(key_prefix="consent", limit=30, window_seconds=60)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _user_agent(request: Request) -> str | None:
    """Truncate to the column width (String(400)) — an untrusted header on this
    public endpoint can be arbitrarily long, and an over-length value would
    raise StringDataRightTruncation → 500 (same [:400] guard the auth module's
    client_info uses before storing a session's user agent)."""
    ua = request.headers.get("user-agent")
    return ua[:400] if ua else None


@public_router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(_consent_limit)])
async def submit_consent(
    data: ConsentIn,
    request: Request,
    tenant: TenantDep,
    service: ComplianceServiceDep,
) -> list[ConsentRecordOut]:
    """Record a visitor's cookie-banner choices as append-only consent proof.
    Anonymous — identified by ``sessionId`` (a signed-in user's own consent view
    is served through the ``/me`` surface)."""
    records = await service.record_banner_choices(
        tenant,
        data,
        user_id=None,
        ip=_client_ip(request),
        user_agent=_user_agent(request),
    )
    return [ConsentRecordOut.model_validate(r) for r in records]


# ---- public: cookie-banner config the frontend renders ----

site_router = APIRouter(prefix="/site", tags=["compliance:public"])


@site_router.get("/cookie-config")
async def get_cookie_config(
    tenant: TenantDep, service: ComplianceServiceDep
) -> CookieConsentConfigOut | None:
    config = await service.get_cookie_config(tenant)
    return CookieConsentConfigOut.model_validate(config) if config else None


# ---- /me: data-subject requests (§10.12) ----

me_router = APIRouter(prefix="/me", tags=["compliance:me"])


@me_router.get("/export")
async def export_my_data(
    tenant: TenantDep, service: ComplianceServiceDep, actor: CurrentUserDep
) -> DataExportOut:
    """Data portability (§10.12): the caller's own data aggregated read-only
    across every module that holds a ``user_id`` / ``contact_id`` for them."""
    data = await service.export_for_user(tenant, actor.id)
    return DataExportOut.model_validate(data)


@me_router.delete("", status_code=status.HTTP_202_ACCEPTED)
async def erase_my_account(
    request: Request,
    tenant: TenantDep,
    service: ComplianceServiceDep,
    auth: AuthServiceDep,
    actor: CurrentUserDep,
) -> ErasureAck:
    """Erasure (§10.12): soft-delete the account now, schedule the 30-day purge.
    Idempotent — a repeat while one is pending returns the existing request."""
    dsr = await service.request_erasure(tenant, actor.id, ip=_client_ip(request))
    # Revoke the caller's live tokens immediately — a soft-deleted account must
    # not keep working off a still-valid 15-min access token (same force-logout
    # the users module does on admin disable/delete).
    assert actor.tenant_id is not None
    await auth.force_logout_user(actor.tenant_id, actor.id)
    assert dsr.purge_scheduled_at is not None
    return ErasureAck(request_id=dsr.id, purge_scheduled_at=dsr.purge_scheduled_at)


@me_router.get("/dsr/{dsr_id}")
async def get_my_dsr(
    dsr_id: uuid.UUID, tenant: TenantDep, service: ComplianceServiceDep, actor: CurrentUserDep
) -> DsrRequestOut:
    dsr = await service.get_dsr(tenant, dsr_id)
    # Ownership check: a DSR belongs to the requesting subject (404, no oracle).
    if dsr.user_id != actor.id:
        raise NotFoundError("Data-subject request not found.")
    return DsrRequestOut.model_validate(dsr)


# ---- portal: cookie config + audit-access report ----

portal_router = APIRouter(
    prefix="/portal/compliance",
    tags=["compliance:portal"],
    dependencies=[Depends(require(Permission.COMPLIANCE_MANAGE))],
)


@portal_router.get("/cookie-config")
async def portal_get_cookie_config(
    tenant: TenantDep, service: ComplianceServiceDep
) -> CookieConsentConfigOut | None:
    config = await service.get_cookie_config(tenant)
    return CookieConsentConfigOut.model_validate(config) if config else None


@portal_router.put("/cookie-config")
async def portal_put_cookie_config(
    data: CookieConsentConfigIn, tenant: TenantDep, service: ComplianceServiceDep
) -> CookieConsentConfigOut:
    config = await service.put_cookie_config(tenant, data)
    return CookieConsentConfigOut.model_validate(config)


@portal_router.get("/audit-log")
async def portal_audit_log(
    tenant: TenantDep,
    session: SessionDep,
    action: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[AuditLogOut]:
    """Audit-access report (§10.11), scoped to the resolved tenant: the
    filterable append-only trail for compliance review. Reads the same
    ``audit_log`` table Part 22's platform report reads, but pinned to *this*
    tenant (a tenant admin never sees another tenant's or platform-only rows)."""
    page_size = clamp_limit(limit)
    after: tuple[datetime, uuid.UUID] | None = None
    if cursor is not None:
        values = decode_cursor(cursor)
        after = (datetime.fromisoformat(values["created_at"]), uuid.UUID(values["id"]))
    repo = AuditRepository(session)
    rows = await repo.list_page(tenant_id=tenant.id, action=action, after=after, limit=page_size)
    items = rows[:page_size]
    next_cursor = None
    if len(rows) > page_size:
        last = items[-1]
        next_cursor = encode_cursor(
            {"created_at": last.created_at.isoformat(), "id": str(last.id)}
        )
    total = await repo.count(tenant_id=tenant.id, action=action)
    return Page(
        items=[AuditLogOut.model_validate(r) for r in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )
