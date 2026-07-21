"""Request/response schemas for transactions (§8.13).

Money matches listings exactly (``price: Decimal`` + a sibling ``currency``,
not a nested money object) — one money representation across the codebase, per
§9's "don't invent a second". Commission figures ride on the same deal shape but
are gated to admins in the service/router (they are sensitive — see the module
docstring), not stripped here.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import Field, model_validator

from app.core.schema import InputSchema, OutSchema, reject_null_for
from app.modules.transactions.models import (
    CommissionBasis,
    DealDocumentStatus,
    DealStatus,
    SignatureStatus,
)

# Money: positive, 2dp, bounded — mirrors listings' PriceField.
MoneyField = Annotated[Decimal, Field(gt=0, le=Decimal("999999999999"), decimal_places=2)]
# Commission rate is a percentage 0-100 with up to 3dp (Numeric(6,3)).
RateField = Annotated[Decimal, Field(ge=0, le=Decimal("100"), decimal_places=3)]

_TITLE = Annotated[str, Field(min_length=1, max_length=255)]


# ---- deals ----


class DealCreate(InputSchema):
    title: _TITLE
    listing_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None  # defaults to the creator
    price: MoneyField | None = None
    currency: str = Field(default="DZD", pattern="^[A-Z]{3}$")
    notes: str | None = Field(default=None, max_length=5000)
    # If true, seed the default milestone checklist on create.
    seed_milestones: bool = True


class DealUpdate(InputSchema):
    title: _TITLE | None = None
    price: MoneyField | None = None
    currency: str | None = Field(default=None, pattern="^[A-Z]{3}$")
    owner_user_id: uuid.UUID | None = None
    listing_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=5000)

    _reject_required_nulls = reject_null_for("title", "currency")


class DealTransition(InputSchema):
    to_status: DealStatus
    # Required when moving to closed_lost (mirrors leads' lost-reason rule).
    lost_reason: str | None = Field(default=None, max_length=500)


class CommissionUpdate(InputSchema):
    """Admin-only (commissions are sensitive). ``basis`` decides which fields
    matter: ``percentage`` computes the amount from the deal price x rate,
    ``flat`` takes the amount directly."""

    basis: CommissionBasis
    rate: RateField | None = None
    amount: MoneyField | None = None

    @model_validator(mode="after")
    def basis_requires_field(self) -> "CommissionUpdate":
        if self.basis is CommissionBasis.PERCENTAGE and self.rate is None:
            raise ValueError("A percentage commission requires a rate.")
        if self.basis is CommissionBasis.FLAT and self.amount is None:
            raise ValueError("A flat commission requires an amount.")
        return self


class DealOut(OutSchema):
    id: uuid.UUID
    owner_user_id: uuid.UUID
    title: str
    status: DealStatus
    listing_id: uuid.UUID | None
    lead_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    price: Decimal | None
    currency: str
    closed_at: datetime | None
    lost_reason: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class DealWithCommissionOut(DealOut):
    """The deal shape an admin sees — commission figures included. Non-admins
    get the plain ``DealOut`` (no commission keys on the wire at all)."""

    commission_basis: CommissionBasis | None
    commission_rate: Decimal | None
    commission_amount: Decimal | None


# ---- milestones ----


class MilestoneCreate(InputSchema):
    title: _TITLE
    due_date: date | None = None
    owner_user_id: uuid.UUID | None = None
    position: int = Field(default=0, ge=0)


class MilestoneUpdate(InputSchema):
    title: _TITLE | None = None
    due_date: date | None = None
    owner_user_id: uuid.UUID | None = None
    position: int | None = Field(default=None, ge=0)
    completed: bool | None = None

    _reject_required_nulls = reject_null_for("title", "position", "completed")


class MilestoneOut(OutSchema):
    id: uuid.UUID
    deal_id: uuid.UUID
    title: str
    due_date: date | None
    owner_user_id: uuid.UUID | None
    completed_at: datetime | None
    position: int
    created_at: datetime
    updated_at: datetime


# ---- documents ----


class DocumentUploadCreate(InputSchema):
    doc_type: str = Field(min_length=1, max_length=60)
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=120)
    size_bytes: int | None = Field(default=None, ge=0)


class DocumentUploadOut(OutSchema):
    """The presigned-PUT response: the client uploads straight to storage, then
    calls confirm."""

    document: "DocumentOut"
    upload_url: str
    headers: dict[str, str]


class DocumentOut(OutSchema):
    id: uuid.UUID
    deal_id: uuid.UUID
    doc_type: str
    filename: str
    content_type: str
    size_bytes: int | None
    sha256: str | None
    status: DealDocumentStatus
    signature_status: SignatureStatus
    uploaded_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class DocumentDownloadOut(OutSchema):
    url: str
