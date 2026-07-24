"""HTTP layer for transactions & deals (§8.13). Portal-only — deals aren't a
public concept.

All routes are gated by ``DEAL_MANAGE`` (agent/team_lead/admin; not marketing).
Ownership/visibility scoping happens in the service via
``AgentsService.scope_user_ids_for``. Commission figures are gated one level
tighter (admin-only) inside the service; the deal serializer here emits the
commission keys only for an admin (``_deal_out``).
"""

import uuid

from fastapi import APIRouter, Depends, Query, status

from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import AuthenticatedUser, Permission, require
from app.core.tenancy import TenantDep
from app.modules.transactions.models import Deal, DealStatus
from app.modules.transactions.schemas import (
    CommissionUpdate,
    DealCreate,
    DealOut,
    DealTransition,
    DealUpdate,
    DealWithCommissionOut,
    DocumentDownloadOut,
    DocumentOut,
    DocumentUploadCreate,
    DocumentUploadOut,
    MilestoneCreate,
    MilestoneOut,
    MilestoneUpdate,
)
from app.modules.transactions.service import TransactionsService, TransactionsServiceDep

portal_router = APIRouter(prefix="/portal/deals", tags=["transactions"])

_DealGuard = require(Permission.DEAL_MANAGE)

# The deal shape is polymorphic: an admin sees commission figures, everyone else
# gets the plain shape. The response_model must be this union (most-specific
# first) so FastAPI serializes each instance by its actual type — annotating a
# bare ``DealOut`` would strip the subclass's commission keys back off.
DealResponse = DealWithCommissionOut | DealOut


def _deal_out(deal: Deal, service: TransactionsService, actor: AuthenticatedUser) -> DealResponse:
    """An admin sees commission figures; everyone else gets the plain shape
    with no commission keys on the wire at all."""
    if service.can_see_commission(actor):
        return DealWithCommissionOut.model_validate(deal)
    return DealOut.model_validate(deal)


# ---- deals ----


@portal_router.post("", status_code=status.HTTP_201_CREATED)
async def create_deal(
    data: DealCreate,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> DealResponse:
    return _deal_out(await service.create_deal(tenant, actor, data), service, actor)


@portal_router.get("")
async def list_deals(
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
    status_filter: DealStatus | None = Query(default=None, alias="status"),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[DealResponse]:
    items, next_cursor, total = await service.list_deals(
        tenant, actor, status=status_filter, cursor=cursor, limit=limit
    )
    return Page(
        items=[_deal_out(d, service, actor) for d in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )


@portal_router.get("/{deal_id}")
async def get_deal(
    deal_id: uuid.UUID,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> DealResponse:
    return _deal_out(await service.get_deal(tenant, actor, deal_id), service, actor)


@portal_router.patch("/{deal_id}")
async def update_deal(
    deal_id: uuid.UUID,
    data: DealUpdate,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> DealResponse:
    return _deal_out(await service.update_deal(tenant, actor, deal_id, data), service, actor)


@portal_router.post("/{deal_id}/status")
async def transition_deal(
    deal_id: uuid.UUID,
    data: DealTransition,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> DealResponse:
    return _deal_out(await service.transition(tenant, actor, deal_id, data), service, actor)


@portal_router.delete("/{deal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_deal(
    deal_id: uuid.UUID,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> None:
    await service.delete_deal(tenant, actor, deal_id)


# ---- commissions (admin-only, enforced in the service) ----


@portal_router.get("/{deal_id}/commission")
async def get_commission(
    deal_id: uuid.UUID,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> DealWithCommissionOut:
    deal = await service.get_deal_with_commission(tenant, actor, deal_id)
    return DealWithCommissionOut.model_validate(deal)


@portal_router.put("/{deal_id}/commission")
async def set_commission(
    deal_id: uuid.UUID,
    data: CommissionUpdate,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> DealWithCommissionOut:
    deal = await service.set_commission(tenant, actor, deal_id, data)
    return DealWithCommissionOut.model_validate(deal)


# ---- milestones ----


@portal_router.get("/{deal_id}/milestones")
async def list_milestones(
    deal_id: uuid.UUID,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> list[MilestoneOut]:
    rows = await service.list_milestones(tenant, actor, deal_id)
    return [MilestoneOut.model_validate(m) for m in rows]


@portal_router.post("/{deal_id}/milestones", status_code=status.HTTP_201_CREATED)
async def add_milestone(
    deal_id: uuid.UUID,
    data: MilestoneCreate,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> MilestoneOut:
    return MilestoneOut.model_validate(await service.add_milestone(tenant, actor, deal_id, data))


@portal_router.patch("/{deal_id}/milestones/{milestone_id}")
async def update_milestone(
    deal_id: uuid.UUID,
    milestone_id: uuid.UUID,
    data: MilestoneUpdate,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> MilestoneOut:
    milestone = await service.update_milestone(tenant, actor, deal_id, milestone_id, data)
    return MilestoneOut.model_validate(milestone)


@portal_router.delete(
    "/{deal_id}/milestones/{milestone_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_milestone(
    deal_id: uuid.UUID,
    milestone_id: uuid.UUID,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> None:
    await service.delete_milestone(tenant, actor, deal_id, milestone_id)


# ---- documents ----


@portal_router.get("/{deal_id}/documents")
async def list_documents(
    deal_id: uuid.UUID,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> list[DocumentOut]:
    rows = await service.list_documents(tenant, actor, deal_id)
    return [DocumentOut.model_validate(d) for d in rows]


@portal_router.post("/{deal_id}/documents/uploads", status_code=status.HTTP_201_CREATED)
async def request_document_upload(
    deal_id: uuid.UUID,
    data: DocumentUploadCreate,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> DocumentUploadOut:
    document, upload_url, headers = await service.request_upload(tenant, actor, deal_id, data)
    return DocumentUploadOut(
        document=DocumentOut.model_validate(document), upload_url=upload_url, headers=headers
    )


@portal_router.post("/{deal_id}/documents/{document_id}/confirm")
async def confirm_document_upload(
    deal_id: uuid.UUID,
    document_id: uuid.UUID,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> DocumentOut:
    document = await service.confirm_upload(tenant, actor, deal_id, document_id)
    return DocumentOut.model_validate(document)


@portal_router.get("/{deal_id}/documents/{document_id}/download")
async def download_document(
    deal_id: uuid.UUID,
    document_id: uuid.UUID,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> DocumentDownloadOut:
    return DocumentDownloadOut(url=await service.download_url(tenant, actor, deal_id, document_id))


@portal_router.delete("/{deal_id}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    deal_id: uuid.UUID,
    document_id: uuid.UUID,
    tenant: TenantDep,
    service: TransactionsServiceDep,
    actor: AuthenticatedUser = Depends(_DealGuard),
) -> None:
    await service.delete_document(tenant, actor, deal_id, document_id)
