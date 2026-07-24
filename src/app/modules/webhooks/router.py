"""HTTP layer for outbound webhooks (§8.14, §10.9).

``/portal/webhooks`` back-office, gated by ``WEBHOOK_MANAGE``: register/edit/
delete endpoints and read the delivery log. No public surface — webhooks are a
tenant-integration concern, and the *inbound* direction (a receiver's endpoint)
lives on the tenant's own infrastructure, not here.
"""

import uuid

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import InvalidWebhookUrlError
from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import AuthenticatedUser, Permission, require
from app.core.tenancy import TenantDep
from app.modules.webhooks.schemas import (
    WebhookDeliveryOut,
    WebhookEndpointCreate,
    WebhookEndpointCreatedOut,
    WebhookEndpointOut,
    WebhookEndpointUpdate,
)
from app.modules.webhooks.service import WebhookServiceDep, WebhookUrlError

portal_router = APIRouter(prefix="/portal/webhooks", tags=["webhooks"])


@portal_router.post("/endpoints", status_code=201)
async def create_endpoint(
    data: WebhookEndpointCreate,
    tenant: TenantDep,
    service: WebhookServiceDep,
    _: AuthenticatedUser = Depends(require(Permission.WEBHOOK_MANAGE)),
) -> WebhookEndpointCreatedOut:
    try:
        endpoint = await service.create_endpoint(tenant, data)
    except WebhookUrlError as exc:
        raise InvalidWebhookUrlError(str(exc)) from exc
    # The one response that carries the plaintext secret (§10.9).
    return WebhookEndpointCreatedOut.model_validate(endpoint)


@portal_router.get("/endpoints")
async def list_endpoints(
    tenant: TenantDep,
    service: WebhookServiceDep,
    _: AuthenticatedUser = Depends(require(Permission.WEBHOOK_MANAGE)),
) -> list[WebhookEndpointOut]:
    endpoints = await service.list_endpoints(tenant)
    return [WebhookEndpointOut.model_validate(e) for e in endpoints]


@portal_router.get("/endpoints/{endpoint_id}")
async def get_endpoint(
    endpoint_id: uuid.UUID,
    tenant: TenantDep,
    service: WebhookServiceDep,
    _: AuthenticatedUser = Depends(require(Permission.WEBHOOK_MANAGE)),
) -> WebhookEndpointOut:
    endpoint = await service.get_endpoint(tenant, endpoint_id)
    return WebhookEndpointOut.model_validate(endpoint)


@portal_router.patch("/endpoints/{endpoint_id}")
async def update_endpoint(
    endpoint_id: uuid.UUID,
    data: WebhookEndpointUpdate,
    tenant: TenantDep,
    service: WebhookServiceDep,
    _: AuthenticatedUser = Depends(require(Permission.WEBHOOK_MANAGE)),
) -> WebhookEndpointOut:
    try:
        endpoint = await service.update_endpoint(tenant, endpoint_id, data)
    except WebhookUrlError as exc:
        raise InvalidWebhookUrlError(str(exc)) from exc
    return WebhookEndpointOut.model_validate(endpoint)


@portal_router.delete("/endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(
    endpoint_id: uuid.UUID,
    tenant: TenantDep,
    service: WebhookServiceDep,
    _: AuthenticatedUser = Depends(require(Permission.WEBHOOK_MANAGE)),
) -> None:
    await service.delete_endpoint(tenant, endpoint_id)


@portal_router.get("/deliveries")
async def list_deliveries(
    tenant: TenantDep,
    service: WebhookServiceDep,
    _: AuthenticatedUser = Depends(require(Permission.WEBHOOK_MANAGE)),
    endpoint_id: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[WebhookDeliveryOut]:
    items, next_cursor = await service.list_deliveries(
        tenant, endpoint_id=endpoint_id, cursor=cursor, limit=limit
    )
    return Page(
        items=[WebhookDeliveryOut.model_validate(d) for d in items], next_cursor=next_cursor
    )
