"""Transactions & deals business logic (§8.13).

Back-office deal tracking once a lead converts: a deal, its milestone checklist,
and its documents. Portal-only (no public surface — deals aren't a public
concept). Visibility reuses the shared ``AgentsService.scope_user_ids_for`` rule
(§8.5): an agent sees their own deals, a team lead their team's, an admin
tenant-wide. Marketing has no ``DEAL_MANAGE`` at all — commissions are sensitive.

Commission figures are gated one level tighter than the rest of the deal: only
an admin may read or set them (``_require_commission_access``). A non-admin's
list/detail responses carry the plain ``DealOut`` with no commission keys; a
non-admin cannot hit the commission endpoint.

The ``listing_id``/``lead_id``/``contact_id`` deal links are column-only — a
client-supplied id is validated through the owning module's boundary accessor
(``ListingService.exists`` / ``LeadsService.lead_exists`` / ``contact_exists``)
before the insert, so a bogus id is a 404, never an FK ``IntegrityError`` → 500.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

import structlog
from fastapi import Depends, Request
from uuid_utils.compat import uuid7

from app.core.config import Settings
from app.core.database import SessionDep, on_commit
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.permissions import AuthenticatedUser, Role
from app.core.storage import ObjectStorage, create_storage
from app.core.tenancy import TenantContext
from app.modules.agents.service import AgentsService, build_agents_boundary
from app.modules.leads.service import LeadsService, get_leads_service
from app.modules.listings.service import ListingService, get_listing_service
from app.modules.transactions.models import (
    CLOSED_STATUSES,
    CommissionBasis,
    Deal,
    DealDocument,
    DealDocumentStatus,
    DealMilestone,
    DealStatus,
)
from app.modules.transactions.repository import TransactionsRepository
from app.modules.transactions.schemas import (
    CommissionUpdate,
    DealCreate,
    DealTransition,
    DealUpdate,
    DocumentUploadCreate,
    MilestoneCreate,
    MilestoneUpdate,
)

logger = structlog.get_logger(__name__)

_CENT = Decimal("0.01")

# The valid deal workflow moves (§8.13). A closed deal is terminal.
_CLOSED = frozenset({DealStatus.CLOSED_WON, DealStatus.CLOSED_LOST})
_TRANSITIONS: dict[DealStatus, frozenset[DealStatus]] = {
    DealStatus.OPEN: _CLOSED | {DealStatus.UNDER_CONTRACT},
    DealStatus.UNDER_CONTRACT: _CLOSED | {DealStatus.OPEN},
    DealStatus.CLOSED_WON: frozenset(),
    DealStatus.CLOSED_LOST: frozenset(),
}

# The checklist seeded on deal create when ``seed_milestones`` is set. A v1
# template — an agency can edit/add/remove afterward.
_DEFAULT_MILESTONES = (
    "Offer accepted",
    "Deposit received",
    "Contract signed",
    "Financing approved",
    "Closing",
)


class TransactionsService:
    def __init__(
        self,
        repo: TransactionsRepository,
        agents: AgentsService,
        listings: ListingService,
        leads: LeadsService,
        storage: ObjectStorage,
    ) -> None:
        self.repo = repo
        self.agents = agents
        self.listings = listings
        self.leads = leads
        self.storage = storage

    # ---- deals ----

    async def _scope(self, tenant_id: uuid.UUID, actor: AuthenticatedUser) -> set[uuid.UUID] | None:
        """None = tenant-wide (admin). Marketing never reaches here (no
        DEAL_MANAGE); team leads see their team, agents see their own."""
        return await self.agents.scope_user_ids_for(tenant_id, actor)

    async def _deal_or_404(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        deal_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> Deal:
        scope = await self._scope(tenant.id, actor)
        deal = await self.repo.get_deal(
            tenant.id, deal_id, scope_user_ids=scope, for_update=for_update
        )
        if deal is None:
            # Out-of-scope deals 404 too — no existence oracle (§8.4 stance).
            raise NotFoundError("Deal not found.")
        return deal

    async def _validate_links(
        self,
        tenant: TenantContext,
        *,
        listing_id: uuid.UUID | None,
        lead_id: uuid.UUID | None,
        contact_id: uuid.UUID | None,
    ) -> None:
        """Client-supplied CRM links must reference rows on this tenant (404
        otherwise) — validated before the FK insert."""
        if listing_id is not None and not await self.listings.exists(tenant.id, listing_id):
            raise NotFoundError("Linked listing not found.")
        if lead_id is not None and not await self.leads.lead_exists(tenant.id, lead_id):
            raise NotFoundError("Linked lead not found.")
        if contact_id is not None and not await self.leads.contact_exists(tenant.id, contact_id):
            raise NotFoundError("Linked contact not found.")

    async def create_deal(
        self, tenant: TenantContext, actor: AuthenticatedUser, data: DealCreate
    ) -> Deal:
        owner_id = data.owner_user_id or actor.id
        # Assigning a deal to someone else is a manager action — a scoped actor
        # (agent/team_lead, i.e. scope is not None) creates deals only for
        # themselves. scope None = tenant-wide (admin) may assign to anyone.
        if owner_id != actor.id and await self._scope(tenant.id, actor) is not None:
            raise PermissionDeniedError("Only a manager can assign a deal to another agent.")

        await self._validate_links(
            tenant, listing_id=data.listing_id, lead_id=data.lead_id, contact_id=data.contact_id
        )

        deal = Deal(
            tenant_id=tenant.id,
            owner_user_id=owner_id,
            title=data.title,
            status=DealStatus.OPEN,
            listing_id=data.listing_id,
            lead_id=data.lead_id,
            contact_id=data.contact_id,
            price=data.price,
            currency=data.currency,
            notes=data.notes,
        )
        self.repo.add_deal(deal)
        await self.repo.flush()

        if data.seed_milestones:
            for position, title in enumerate(_DEFAULT_MILESTONES):
                self.repo.add_milestone(
                    DealMilestone(
                        tenant_id=tenant.id, deal_id=deal.id, title=title, position=position
                    )
                )
            await self.repo.flush()
        return deal

    async def get_deal(
        self, tenant: TenantContext, actor: AuthenticatedUser, deal_id: uuid.UUID
    ) -> Deal:
        return await self._deal_or_404(tenant, actor, deal_id)

    async def list_deals(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        *,
        status: DealStatus | None,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[Deal], str | None, int]:
        page_size = clamp_limit(limit)
        scope = await self._scope(tenant.id, actor)
        after = _decode_keyset(cursor) if cursor else None
        rows = await self.repo.list_deals(
            tenant.id, scope_user_ids=scope, status=status, after=after, limit=page_size
        )
        items = rows[:page_size]
        next_cursor = _next_cursor(rows, items, page_size)
        total = await self.repo.count_deals(tenant.id, scope_user_ids=scope, status=status)
        return items, next_cursor, total

    async def update_deal(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        deal_id: uuid.UUID,
        data: DealUpdate,
    ) -> Deal:
        deal = await self._deal_or_404(tenant, actor, deal_id, for_update=True)
        patch = data.model_dump(exclude_unset=True)

        # Reassigning the owner is a manager-only action (tenant-wide scope).
        reassigning = "owner_user_id" in patch and patch["owner_user_id"] != deal.owner_user_id
        if reassigning and await self._scope(tenant.id, actor) is not None:
            raise PermissionDeniedError("Only a manager can reassign a deal.")

        # Re-validate any link the patch touches.
        await self._validate_links(
            tenant,
            listing_id=patch.get("listing_id"),
            lead_id=patch.get("lead_id"),
            contact_id=patch.get("contact_id"),
        )
        for field, value in patch.items():
            setattr(deal, field, value)
        await self.repo.flush()
        return deal

    async def transition(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        deal_id: uuid.UUID,
        data: DealTransition,
    ) -> Deal:
        deal = await self._deal_or_404(tenant, actor, deal_id, for_update=True)
        target = data.to_status
        if target == deal.status:
            return deal  # idempotent no-op
        if target not in _TRANSITIONS[deal.status]:
            raise ConflictError(
                f"Cannot move a deal from '{deal.status.value}' to '{target.value}'."
            )
        if target is DealStatus.CLOSED_LOST and not data.lost_reason:
            raise ConflictError("A lost deal requires a reason.")

        deal.status = target
        if target in CLOSED_STATUSES:
            deal.closed_at = datetime.now(UTC)
            deal.lost_reason = data.lost_reason if target is DealStatus.CLOSED_LOST else None
        else:
            # Relisting an accidentally-closed deal clears the close stamps.
            deal.closed_at = None
            deal.lost_reason = None
        await self.repo.flush()
        return deal

    async def delete_deal(
        self, tenant: TenantContext, actor: AuthenticatedUser, deal_id: uuid.UUID
    ) -> None:
        deal = await self._deal_or_404(tenant, actor, deal_id, for_update=True)
        # Collect document objects to purge post-commit (mirrors media delete).
        documents = await self.repo.list_documents(tenant.id, deal.id)
        keys = [d.storage_key for d in documents]
        await self.repo.delete_deal(deal)  # milestones/documents cascade
        await self.repo.flush()
        if keys:
            self._purge_objects(keys)

    # ---- commissions (admin-only) ----

    def _require_commission_access(self, actor: AuthenticatedUser) -> None:
        """Commission figures are admin-only — a field-level gate on top of
        DEAL_MANAGE (an agent manages the deal but not its money)."""
        if actor.role is not Role.ADMIN:
            raise PermissionDeniedError("Only an admin can view or set commission figures.")

    async def get_deal_with_commission(
        self, tenant: TenantContext, actor: AuthenticatedUser, deal_id: uuid.UUID
    ) -> Deal:
        self._require_commission_access(actor)
        return await self._deal_or_404(tenant, actor, deal_id)

    async def set_commission(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        deal_id: uuid.UUID,
        data: CommissionUpdate,
    ) -> Deal:
        self._require_commission_access(actor)
        deal = await self._deal_or_404(tenant, actor, deal_id, for_update=True)
        deal.commission_basis = data.basis
        if data.basis is CommissionBasis.PERCENTAGE:
            deal.commission_rate = data.rate
            # Derive the amount from the deal price when both are known; the
            # figure is informational until a price exists.
            if deal.price is not None and data.rate is not None:
                deal.commission_amount = (deal.price * data.rate / 100).quantize(
                    _CENT, rounding=ROUND_HALF_UP
                )
            else:
                deal.commission_amount = None
        else:  # FLAT
            deal.commission_rate = None
            deal.commission_amount = data.amount
        await self.repo.flush()
        return deal

    def can_see_commission(self, actor: AuthenticatedUser) -> bool:
        return actor.role is Role.ADMIN

    # ---- milestones ----

    async def list_milestones(
        self, tenant: TenantContext, actor: AuthenticatedUser, deal_id: uuid.UUID
    ) -> list[DealMilestone]:
        await self._deal_or_404(tenant, actor, deal_id)
        return await self.repo.list_milestones(tenant.id, deal_id)

    async def add_milestone(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        deal_id: uuid.UUID,
        data: MilestoneCreate,
    ) -> DealMilestone:
        await self._deal_or_404(tenant, actor, deal_id)
        milestone = DealMilestone(
            tenant_id=tenant.id,
            deal_id=deal_id,
            title=data.title,
            due_date=data.due_date,
            owner_user_id=data.owner_user_id,
            position=data.position,
        )
        self.repo.add_milestone(milestone)
        await self.repo.flush()
        return milestone

    async def update_milestone(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        deal_id: uuid.UUID,
        milestone_id: uuid.UUID,
        data: MilestoneUpdate,
    ) -> DealMilestone:
        await self._deal_or_404(tenant, actor, deal_id)
        milestone = await self.repo.get_milestone(tenant.id, deal_id, milestone_id, for_update=True)
        if milestone is None:
            raise NotFoundError("Milestone not found.")
        patch = data.model_dump(exclude_unset=True)
        completed = patch.pop("completed", None)
        if completed is not None:
            # Toggling completion (re)stamps completed_at; a due_date change on a
            # still-open milestone clears the reminder stamp so a rescheduled
            # milestone can be reminded again.
            milestone.completed_at = datetime.now(UTC) if completed else None
        for field, value in patch.items():
            setattr(milestone, field, value)
        if "due_date" in patch and milestone.completed_at is None:
            milestone.reminder_sent_at = None
        await self.repo.flush()
        return milestone

    async def delete_milestone(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        deal_id: uuid.UUID,
        milestone_id: uuid.UUID,
    ) -> None:
        await self._deal_or_404(tenant, actor, deal_id)
        milestone = await self.repo.get_milestone(tenant.id, deal_id, milestone_id, for_update=True)
        if milestone is None:
            raise NotFoundError("Milestone not found.")
        await self.repo.delete_milestone(milestone)
        await self.repo.flush()

    # ---- documents ----

    async def list_documents(
        self, tenant: TenantContext, actor: AuthenticatedUser, deal_id: uuid.UUID
    ) -> list[DealDocument]:
        await self._deal_or_404(tenant, actor, deal_id)
        return await self.repo.list_documents(tenant.id, deal_id)

    async def request_upload(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        deal_id: uuid.UUID,
        data: DocumentUploadCreate,
    ) -> tuple[DealDocument, str, dict[str, str]]:
        """Presign a PUT straight to the private bucket — the file never passes
        through FastAPI (§8.2). Confirm computes the sha256 server-side."""
        await self._deal_or_404(tenant, actor, deal_id)
        document_id = uuid7()
        storage_key = f"tenants/{tenant.id}/deals/{deal_id}/documents/{document_id}"
        document = DealDocument(
            id=document_id,
            tenant_id=tenant.id,
            deal_id=deal_id,
            doc_type=data.doc_type,
            filename=data.filename,
            content_type=data.content_type,
            storage_key=storage_key,
            size_bytes=data.size_bytes,
            status=DealDocumentStatus.PENDING,
            uploaded_by=actor.id,
        )
        self.repo.add_document(document)
        await self.repo.flush()
        upload_url = self.storage.presign_put(
            self.storage.docs_bucket, storage_key, data.content_type
        )
        return document, upload_url, {"Content-Type": data.content_type}

    async def confirm_upload(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        deal_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> DealDocument:
        """Verify the object landed, record its real size + server-computed
        sha256 (never trust the client's claim — Part 6 stance), flip to
        ``ready``. Runs the hash inline: deal documents are small back-office
        files (contracts/PDFs), not the large media the async pipeline exists
        for, so a worker round-trip would only add latency."""
        await self._deal_or_404(tenant, actor, deal_id)
        document = await self.repo.get_document(tenant.id, deal_id, document_id, for_update=True)
        if document is None:
            raise NotFoundError("Document not found.")
        if document.status is not DealDocumentStatus.PENDING:
            raise ConflictError(f"This upload is already '{document.status.value}'.")

        try:
            size = self.storage.object_size(self.storage.docs_bucket, document.storage_key)
            body = self.storage.get_object(self.storage.docs_bucket, document.storage_key)
        except Exception:
            document.status = DealDocumentStatus.FAILED
            await self.repo.flush()
            raise ConflictError("No uploaded file was found for this document.") from None

        document.size_bytes = size
        document.sha256 = hashlib.sha256(body).hexdigest()
        document.status = DealDocumentStatus.READY
        await self.repo.flush()
        return document

    async def download_url(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        deal_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> str:
        await self._deal_or_404(tenant, actor, deal_id)
        document = await self.repo.get_document(tenant.id, deal_id, document_id)
        if document is None:
            raise NotFoundError("Document not found.")
        if document.status is not DealDocumentStatus.READY:
            raise ConflictError("This document is not ready for download.")
        return self.storage.presign_get(
            self.storage.docs_bucket, document.storage_key, filename=document.filename
        )

    async def delete_document(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        deal_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> None:
        await self._deal_or_404(tenant, actor, deal_id)
        document = await self.repo.get_document(tenant.id, deal_id, document_id, for_update=True)
        if document is None:
            raise NotFoundError("Document not found.")
        key = document.storage_key
        await self.repo.delete_document(document)
        await self.repo.flush()
        self._purge_objects([key])

    def _purge_objects(self, keys: list[str]) -> None:
        """Delete private-bucket objects after commit — a rolled-back delete
        must not orphan-delete live files (mirrors media's delete)."""
        storage = self.storage
        bucket = storage.docs_bucket

        async def _purge() -> None:
            # Lazy import to keep the module-load graph acyclic (task ->
            # storage). Reuse the media cleanup task — it's a generic
            # bucket/key object delete.
            from app.workers.tasks.media import delete_media_objects

            delete_media_objects.delay([[bucket, key] for key in keys])

        on_commit(self.repo.session, _purge)


def _next_cursor(rows: list[Deal], items: list[Deal], page_size: int) -> str | None:
    if len(rows) <= page_size:
        return None
    last = items[-1]
    return encode_cursor({"created_at": last.created_at.isoformat(), "id": str(last.id)})


def _decode_keyset(cursor: str) -> tuple[datetime, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(values["created_at"]), uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def get_transactions_service(session: SessionDep, request: Request) -> TransactionsService:
    settings: Settings = request.app.state.settings
    return TransactionsService(
        TransactionsRepository(session),
        build_agents_boundary(session),
        get_listing_service(session),
        get_leads_service(session),
        create_storage(settings),
    )


TransactionsServiceDep = Annotated[TransactionsService, Depends(get_transactions_service)]
