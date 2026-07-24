"""HTTP layer for the tenants module.

- ``platform_router`` — platform back-office CRUD + lifecycle/billing/admin
  (tenant-exempt, platform-RBAC-guarded).
- ``site_router`` — public per-tenant endpoints (``GET /site/config``).
- ``billing_webhook_router`` — the provider webhook receiver (tenant-exempt, no
  auth — verification is by signature per §10.9).
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.database import SessionDep
from app.core.exceptions import InvalidWebhookError
from app.core.idempotency import IdempotentRoute
from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.pagination import decode_cursor as _decode_cursor
from app.core.pagination import encode_cursor as _encode_cursor
from app.core.permissions import CurrentUserDep, Permission, require
from app.core.tenancy import TenantDep
from app.integrations.billing.base import WebhookVerificationError
from app.modules.tenants.admin import build_platform_admin_service
from app.modules.tenants.audit import AuditActor, AuditRepository
from app.modules.tenants.billing import build_billing_service
from app.modules.tenants.models import TenantStatus
from app.modules.tenants.plans import plan_limits
from app.modules.tenants.schemas import (
    AuditLogOut,
    CheckoutCreate,
    CheckoutOut,
    DomainVerifyOut,
    ImpersonationOut,
    PlanLimitsOut,
    PlatformMetricsOut,
    SiteConfigOut,
    SubscriptionOut,
    TenantCreate,
    TenantDomainCreate,
    TenantMetricRow,
    TenantOut,
    TenantPlanUpdate,
    TenantUpdate,
    UsageOut,
    WebhookAck,
)
from app.modules.tenants.service import TenantServiceDep

# Reads need PLATFORM_TENANT_VIEW (router-wide); mutations add PLATFORM_TENANT_MANAGE.
_view = [Depends(require(Permission.PLATFORM_TENANT_VIEW))]
platform_router = APIRouter(
    prefix="/platform/tenants",
    tags=["platform:tenants"],
    dependencies=_view,
)
_manage = Depends(require(Permission.PLATFORM_TENANT_MANAGE))
# Same prefix/RBAC as platform_router — shares the *same* `_view` list object
# rather than a second `Depends(require(...))` literal, so tightening the
# gate on one can't silently drift from the other — split off only so the
# checkout POST gets Idempotency-Key handling (§9): a retried checkout call
# must not open a second billing-provider session for the same tenant.
platform_billing_idempotent_router = APIRouter(
    prefix="/platform/tenants",
    tags=["platform:tenants"],
    dependencies=_view,
    route_class=IdempotentRoute,
)


@platform_router.post("", status_code=status.HTTP_201_CREATED, dependencies=[_manage])
async def create_tenant(data: TenantCreate, service: TenantServiceDep) -> TenantOut:
    return TenantOut.model_validate(await service.create(data))


@platform_router.get("")
async def list_tenants(
    service: TenantServiceDep,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[TenantOut]:
    items, next_cursor, total = await service.list(cursor=cursor, limit=limit)
    return Page(
        items=[TenantOut.model_validate(t) for t in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )


@platform_router.get("/{tenant_id}")
async def get_tenant(tenant_id: uuid.UUID, service: TenantServiceDep) -> TenantOut:
    return TenantOut.model_validate(await service.get(tenant_id))


@platform_router.patch("/{tenant_id}", dependencies=[_manage])
async def update_tenant(
    tenant_id: uuid.UUID, data: TenantUpdate, service: TenantServiceDep
) -> TenantOut:
    return TenantOut.model_validate(await service.update(tenant_id, data))


@platform_router.post("/{tenant_id}/suspend", dependencies=[_manage])
async def suspend_tenant(tenant_id: uuid.UUID, service: TenantServiceDep) -> TenantOut:
    return TenantOut.model_validate(await service.set_status(tenant_id, TenantStatus.SUSPENDED))


@platform_router.post("/{tenant_id}/activate", dependencies=[_manage])
async def activate_tenant(tenant_id: uuid.UUID, service: TenantServiceDep) -> TenantOut:
    return TenantOut.model_validate(await service.set_status(tenant_id, TenantStatus.ACTIVE))


@platform_router.post(
    "/{tenant_id}/domains", status_code=status.HTTP_201_CREATED, dependencies=[_manage]
)
async def add_domain(
    tenant_id: uuid.UUID, data: TenantDomainCreate, service: TenantServiceDep
) -> TenantOut:
    return TenantOut.model_validate(await service.add_domain(tenant_id, data))


@platform_router.delete("/{tenant_id}/domains/{domain_id}", dependencies=[_manage])
async def remove_domain(
    tenant_id: uuid.UUID, domain_id: uuid.UUID, service: TenantServiceDep
) -> TenantOut:
    return TenantOut.model_validate(await service.remove_domain(tenant_id, domain_id))


@platform_router.post("/{tenant_id}/domains/{domain_id}/verify", dependencies=[_manage])
async def verify_domain(
    tenant_id: uuid.UUID, domain_id: uuid.UUID, service: TenantServiceDep
) -> DomainVerifyOut:
    """Check the domain's DNS TXT challenge and return the resulting status."""
    domain = await service.verify_domain(tenant_id, domain_id)
    return DomainVerifyOut.model_validate(domain)


# ---- plan / lifecycle (§8.16) ----


@platform_router.put("/{tenant_id}/plan", dependencies=[_manage])
async def set_plan(
    tenant_id: uuid.UUID, data: TenantPlanUpdate, service: TenantServiceDep
) -> TenantOut:
    return TenantOut.model_validate(await service.set_plan(tenant_id, data.plan))


@platform_router.post("/{tenant_id}/offboard", dependencies=[_manage])
async def offboard_tenant(tenant_id: uuid.UUID, service: TenantServiceDep) -> TenantOut:
    """Begin offboarding: suspend now, export the data, schedule the purge."""
    return TenantOut.model_validate(await service.start_offboard(tenant_id))


@platform_router.post("/{tenant_id}/offboard/cancel", dependencies=[_manage])
async def cancel_offboard(tenant_id: uuid.UUID, service: TenantServiceDep) -> TenantOut:
    return TenantOut.model_validate(await service.cancel_offboard(tenant_id))


# ---- billing (§8.16) ----


@platform_router.get("/{tenant_id}/subscription")
async def get_subscription(
    tenant_id: uuid.UUID, session: SessionDep, request: Request
) -> SubscriptionOut | None:
    service = build_billing_service(session, request.app.state.redis)
    subscription = await service.get_subscription(tenant_id)
    return SubscriptionOut.model_validate(subscription) if subscription else None


@platform_billing_idempotent_router.post(
    "/{tenant_id}/checkout", status_code=status.HTTP_201_CREATED, dependencies=[_manage]
)
async def start_checkout(
    tenant_id: uuid.UUID, data: CheckoutCreate, session: SessionDep, request: Request
) -> CheckoutOut:
    service = build_billing_service(session, request.app.state.redis)
    checkout = await service.start_checkout(tenant_id, data.plan, data.customer_email)
    return CheckoutOut(url=checkout.url, session_id=checkout.session_id)


# ---- platform admin: impersonation + cross-tenant metrics (§8.16/§10.11) ----


@platform_router.post("/{tenant_id}/impersonate", dependencies=[_manage])
async def impersonate_tenant(
    tenant_id: uuid.UUID, user: CurrentUserDep, session: SessionDep, request: Request
) -> ImpersonationOut:
    """Mint a time-boxed, audit-logged impersonation token for the tenant's
    admin. The response's ``impersonation`` flag (and the token's ``imp`` claim)
    is the frontend's "impersonation active" banner signal."""
    service = build_platform_admin_service(session)
    actor = AuditActor(
        user_id=user.id,
        role=user.role.value,
        ip=request.client.host if request.client else None,
    )
    grant = await service.impersonate(tenant_id, actor=actor)
    return ImpersonationOut(
        access_token=grant.access_token,
        expires_in=grant.expires_in,
        tenant_id=grant.tenant_id,
        tenant_slug=grant.tenant_slug,
        acting_as_user_id=grant.acting_as_user_id,
    )


platform_admin_router = APIRouter(
    prefix="/platform",
    tags=["platform:admin"],
    dependencies=[Depends(require(Permission.PLATFORM_TENANT_VIEW))],
)


@platform_admin_router.get("/metrics")
async def platform_metrics(session: SessionDep) -> PlatformMetricsOut:
    """Cross-tenant snapshot from the O(1) usage counters (§8.16)."""
    service = build_platform_admin_service(session)
    metrics = await service.metrics()
    return PlatformMetricsOut(
        total_tenants=metrics.total_tenants,
        active_tenants=metrics.active_tenants,
        trial_tenants=metrics.trial_tenants,
        suspended_tenants=metrics.suspended_tenants,
        total_listings=metrics.total_listings,
        total_agents=metrics.total_agents,
        tenants=[
            TenantMetricRow(
                tenant_id=row.tenant_id,
                tenant_name=row.tenant_name,
                status=row.status,
                plan=row.plan,
                listings_count=row.listings_count,
                agents_count=row.agents_count,
                storage_bytes=row.storage_bytes,
            )
            for row in metrics.tenants
        ],
    )


@platform_admin_router.get("/audit-log")
async def list_audit_log(
    session: SessionDep,
    tenant_id: uuid.UUID | None = Query(default=None),
    action: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[AuditLogOut]:
    """Audit-access report (§10.11): filterable append-only trail. Part 23
    broadens the write sites; this is the read/report surface."""
    from app.core.pagination import clamp_limit

    page_size = clamp_limit(limit)
    after: tuple[datetime, uuid.UUID] | None = None
    if cursor is not None:
        values = _decode_cursor(cursor)
        after = (datetime.fromisoformat(values["created_at"]), uuid.UUID(values["id"]))
    repo = AuditRepository(session)
    rows = await repo.list_page(tenant_id=tenant_id, action=action, after=after, limit=page_size)
    items = rows[:page_size]
    next_cursor = None
    if len(rows) > page_size:
        last = items[-1]
        next_cursor = _encode_cursor(
            {"created_at": last.created_at.isoformat(), "id": str(last.id)}
        )
    total = await repo.count(tenant_id=tenant_id, action=action)
    return Page(
        items=[AuditLogOut.model_validate(r) for r in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )


site_router = APIRouter(prefix="/site", tags=["site"])


@site_router.get("/config")
async def get_site_config(tenant: TenantDep, service: TenantServiceDep) -> SiteConfigOut:
    """Public branding/config for the resolved tenant. Part 22 (§8.16) adds the
    plan + current usage + limits so the dashboard shows quota consumption in
    one call; the usage read is the only DB hit (the rest is the cached
    context)."""
    snapshot = await service.usage_snapshot(tenant.id)
    limits = plan_limits(tenant.plan)
    return SiteConfigOut(
        name=tenant.name,
        slug=tenant.slug,
        settings=tenant.settings,
        plan=tenant.plan,
        usage=UsageOut(
            listings_count=snapshot.listings_count,
            agents_count=snapshot.agents_count,
            storage_bytes=snapshot.storage_bytes,
            emails_sent=snapshot.emails_sent,
        ),
        limits=PlanLimitsOut(
            max_listings=limits.max_listings,
            max_agents=limits.max_agents,
            storage_gb=limits.storage_gb,
            monthly_emails=limits.monthly_emails,
        ),
    )


# ---- billing webhook receiver (§10.9) ----

billing_webhook_router = APIRouter(prefix="/billing", tags=["billing"])


@billing_webhook_router.post("/webhook", status_code=status.HTTP_200_OK)
async def billing_webhook(
    request: Request, session: SessionDep, signature: str = Query(default="", alias="signature")
) -> WebhookAck:
    """Provider webhook receiver. Verification is by signature + freshness
    (§10.9), not auth; a bad/stale signature is a 400 and is never processed.
    Tenant-exempt: mounted outside any tenant context.

    The signature is read from the ``X-Billing-Signature`` header (a provider
    sends it there); a query fallback exists for providers that cannot set
    headers on their test tooling."""
    header_sig = request.headers.get("x-billing-signature", "") or signature
    payload = await request.body()
    service = build_billing_service(session, request.app.state.redis)
    try:
        received, processed = await service.handle_webhook(payload=payload, signature=header_sig)
    except WebhookVerificationError as exc:
        raise InvalidWebhookError("The webhook signature could not be verified.") from exc
    return WebhookAck(received=received, processed=processed)
