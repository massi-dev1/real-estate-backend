"""HTTP layer for valuations (§8.8) — entirely public, unauthenticated.

- The multi-step seller form: start (address) → details → complete (contact,
  capture-defended). Steps 2/3 address the row via the HMAC token step 1
  returned. Agency-side visibility is the existing lead inbox — there is no
  portal surface here.
- The stateless mortgage calculator and its "email me this estimate" variant.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, status

from app.core.rate_limit import rate_limit
from app.core.tenancy import TenantDep
from app.modules.valuations.models import ValuationRequest
from app.modules.valuations.schemas import (
    MortgageEmailCreate,
    MortgageEmailOut,
    MortgageEstimateIn,
    MortgageEstimateOut,
    ValuationCompleteCreate,
    ValuationDetailsUpdate,
    ValuationDraftOut,
    ValuationEstimateOut,
    ValuationStartCreate,
    ValuationTokenOut,
)
from app.modules.valuations.service import DISCLAIMER, ValuationsServiceDep

public_router = APIRouter(tags=["valuations:public"])

# One form spans three requests — the bucket is per-flow generous but still
# kills scripted floods (same tenant+IP keying as every capture surface).
_valuation_limit = rate_limit(key_prefix="valuation", limit=15, window_seconds=3600)
# The calculator is called from every listing detail page — cheap and pure,
# so the bucket only has to stop abuse, not meter usage.
_mortgage_limit = rate_limit(key_prefix="mortgage", limit=60, window_seconds=60)
_mortgage_email_limit = rate_limit(key_prefix="mortgage_email", limit=5, window_seconds=60)


def _estimate_out(row: ValuationRequest) -> ValuationEstimateOut:
    return ValuationEstimateOut(
        id=row.id,
        estimate_low=row.estimate_low,
        estimate_high=row.estimate_high,
        currency=row.currency,
        comps_count=row.comps_count or 0,
        completed_at=row.completed_at or datetime.now(UTC),
        disclaimer=DISCLAIMER,
    )


@public_router.post(
    "/valuations",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_valuation_limit)],
)
async def start_valuation(
    data: ValuationStartCreate, tenant: TenantDep, service: ValuationsServiceDep
) -> ValuationTokenOut:
    _, token = await service.start(tenant, data)
    return ValuationTokenOut(token=token)


@public_router.patch("/valuations/{token}", dependencies=[Depends(_valuation_limit)])
async def update_valuation_details(
    token: str, data: ValuationDetailsUpdate, tenant: TenantDep, service: ValuationsServiceDep
) -> ValuationDraftOut:
    row = await service.update_details(tenant, token, data)
    return ValuationDraftOut.model_validate(row)


@public_router.post("/valuations/{token}/complete", dependencies=[Depends(_valuation_limit)])
async def complete_valuation(
    token: str, data: ValuationCompleteCreate, tenant: TenantDep, service: ValuationsServiceDep
) -> ValuationEstimateOut:
    row = await service.complete(tenant, token, data)
    if row is None:
        # Honeypot: a real-shaped null band — indistinguishable from a
        # low-data area — and nothing persisted.
        return ValuationEstimateOut(
            id=uuid.uuid4(),
            estimate_low=None,
            estimate_high=None,
            currency="DZD",
            comps_count=0,
            completed_at=datetime.now(UTC),
            disclaimer=DISCLAIMER,
        )
    return _estimate_out(row)


@public_router.post("/tools/mortgage-estimate", dependencies=[Depends(_mortgage_limit)])
async def mortgage_estimate(
    data: MortgageEstimateIn, tenant: TenantDep, service: ValuationsServiceDep
) -> MortgageEstimateOut:
    return service.mortgage_estimate(tenant, data)


@public_router.post(
    "/tools/mortgage-estimate/email",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_mortgage_email_limit)],
)
async def mortgage_estimate_email(
    data: MortgageEmailCreate, tenant: TenantDep, service: ValuationsServiceDep
) -> MortgageEmailOut:
    lead, estimate = await service.mortgage_email(tenant, data)
    # Same honeypot camouflage as every capture: a real-shaped id, no lead.
    return MortgageEmailOut(id=lead.id if lead is not None else uuid.uuid4(), estimate=estimate)
