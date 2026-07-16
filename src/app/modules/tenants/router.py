"""HTTP layer for the tenants module.

- ``platform_router`` — platform back-office CRUD (tenant-exempt, key-guarded).
- ``site_router`` — public per-tenant endpoints (``GET /site/config``).
"""

import uuid

from fastapi import APIRouter, Depends, Query, status

from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import Permission, require
from app.core.tenancy import TenantDep
from app.modules.tenants.models import TenantStatus
from app.modules.tenants.schemas import (
    SiteConfigOut,
    TenantCreate,
    TenantDomainCreate,
    TenantOut,
    TenantUpdate,
)
from app.modules.tenants.service import TenantServiceDep

# Reads need PLATFORM_TENANT_VIEW (router-wide); mutations add PLATFORM_TENANT_MANAGE.
platform_router = APIRouter(
    prefix="/platform/tenants",
    tags=["platform:tenants"],
    dependencies=[Depends(require(Permission.PLATFORM_TENANT_VIEW))],
)
_manage = Depends(require(Permission.PLATFORM_TENANT_MANAGE))


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


site_router = APIRouter(prefix="/site", tags=["site"])


@site_router.get("/config")
async def get_site_config(tenant: TenantDep) -> SiteConfigOut:
    """Public branding/config for the resolved tenant — served from the
    middleware's context, no DB hit."""
    return SiteConfigOut(name=tenant.name, slug=tenant.slug, settings=tenant.settings)
