"""Pydantic schemas for valuations (§8.8).

The multi-step form maps to three payloads — start (address), details, and
complete — the last of which subclasses the leads module's ``_CaptureBase`` so
the contact step carries exactly the same spam defense as every other public
capture surface. The mortgage calculator is a stateless input/output pair; its
"email me" variant is capture-defended the same way.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import Field, model_validator

from app.core.schema import InputSchema, OutSchema
from app.modules.leads.schemas import _CaptureBase
from app.modules.listings.models import PropertyType

MAX_TERM_YEARS = 40


class ValuationStartCreate(InputSchema):
    """Step 1 — where the property is. The map-pin point is optional but it
    is the only geo signal (no geocoding exists): without it the estimate
    stays null and the lead routes to an agent conversation."""

    street: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=1, max_length=120)
    postal_code: str | None = Field(default=None, max_length=20)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def point_complete(self) -> Self:
        if (self.lat is None) != (self.lng is None):
            raise ValueError("provide both lat and lng or neither")
        return self


class ValuationTokenOut(OutSchema):
    token: str


class ValuationDetailsUpdate(InputSchema):
    """Step 2 — what the property is. Partial and repeatable; only provided
    fields change. Free-text extras are bounded and land in the ``details``
    JSONB — stored for the agent, never queried."""

    property_type: PropertyType | None = None
    area_built: Decimal | None = Field(default=None, gt=0, le=Decimal("99999999.99"))
    beds: int | None = Field(default=None, ge=0, le=100)
    baths: int | None = Field(default=None, ge=0, le=100)
    floor: int | None = Field(default=None, ge=-5, le=200)
    year_built: int | None = Field(default=None, ge=1800, le=2100)
    condition: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=2000)


class ValuationDraftOut(OutSchema):
    address: dict[str, Any]
    property_type: PropertyType | None
    area_built: Decimal | None
    beds: int | None
    baths: int | None
    floor: int | None
    year_built: int | None
    details: dict[str, Any]


class ValuationCompleteCreate(_CaptureBase):
    """Step 3 — who to talk to. Nothing beyond the shared capture shape: the
    property is already on the request row, the source is fixed server-side."""


class ValuationEstimateOut(OutSchema):
    id: uuid.UUID
    estimate_low: Decimal | None
    estimate_high: Decimal | None
    currency: str
    comps_count: int
    completed_at: datetime
    # §8.8: the range is a conversation starter, never an appraisal — the
    # wording ships with the payload so no client can forget it.
    disclaimer: str


class MortgageEstimateIn(InputSchema):
    price: Decimal = Field(gt=0, le=Decimal("999999999999.99"))
    down_payment: Decimal | None = Field(default=None, ge=0)
    annual_rate_percent: Decimal | None = Field(default=None, ge=0, le=100)
    term_years: int | None = Field(default=None, ge=1, le=MAX_TERM_YEARS)

    @model_validator(mode="after")
    def down_payment_below_price(self) -> Self:
        if self.down_payment is not None and self.down_payment >= self.price:
            raise ValueError("downPayment must be below price")
        return self


class MortgageEstimateOut(OutSchema):
    price: Decimal
    down_payment: Decimal
    loan_amount: Decimal
    annual_rate_percent: Decimal
    term_years: int
    monthly_payment: Decimal
    total_paid: Decimal
    total_interest: Decimal


class MortgageEmailCreate(MortgageEstimateIn, _CaptureBase):
    """"Email me this estimate" — the calculator inputs plus the shared
    capture shape; the recipient address is the contact's email."""

    @model_validator(mode="after")
    def email_required(self) -> Self:
        # A honeypot hit (hp filled) must reach the router's camouflaged 201,
        # never a distinguishable 422 — so only genuine submissions are held
        # to the "email needed to email you" rule.
        if not self.hp and not self.contact.email:
            raise ValueError("contact.email is required to email an estimate")
        return self


class MortgageEmailOut(OutSchema):
    id: uuid.UUID
    estimate: MortgageEstimateOut
