"""Valuations business logic (§8.8): the multi-step seller form, the
comparable-sales estimate band, and the mortgage/affordability calculator.

The anonymous step flow is held together by a stateless HMAC capability token
(``sign_value``, purpose-separated, value pinned to tenant + row id) — a
forged or foreign-tenant token 404s, and nothing has to outlive a Redis TTL.
The estimate is deliberately coarse: an interquartile price/m² band over
nearby published/sold sale comps. The goal is the agent conversation, not
algorithmic precision — too little data yields a null band and the lead still
lands in the CRM.
"""

import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any

import structlog
from fastapi import Depends, Request

from app.common.geo import point_lonlat, to_point
from app.core.config import Settings
from app.core.database import SessionDep, on_commit
from app.core.exceptions import ConflictError, NotFoundError
from app.core.security import sign_value, unsign_value
from app.core.tenancy import TenantContext
from app.modules.leads.models import Lead
from app.modules.leads.service import LeadsService, get_leads_service
from app.modules.listings.service import ListingService, get_listing_service
from app.modules.valuations.models import ValuationRequest
from app.modules.valuations.repository import ValuationsRepository
from app.modules.valuations.schemas import (
    MortgageEmailCreate,
    MortgageEstimateIn,
    MortgageEstimateOut,
    ValuationCompleteCreate,
    ValuationDetailsUpdate,
    ValuationStartCreate,
)
from app.workers.tasks.email import send_email

logger = structlog.get_logger(__name__)

_TOKEN_PURPOSE = "valuation-request"

# Comp search: widen ring by ring until enough comps, then stop — a dense
# neighborhood prices on 2 km, a rural property on 10.
RADIUS_LADDER_KM = (2.0, 5.0, 10.0)
MIN_COMPS = 3
COMPS_LIMIT = 50

DISCLAIMER = (
    "This is an automated estimate range based on comparable listings, "
    "not an appraisal. An agent will contact you for a precise valuation."
)

# Mortgage defaults when the tenant hasn't configured settings.mortgage.
DEFAULT_ANNUAL_RATE_PERCENT = Decimal("6.5")
DEFAULT_TERM_YEARS = 25
DEFAULT_DOWN_PAYMENT_PERCENT = Decimal("20")

_CENT = Decimal("0.01")


def _tenant_mortgage_settings(tenant: TenantContext) -> tuple[Decimal, int, Decimal]:
    """(annual_rate_percent, term_years, down_payment_percent) — free-form
    JSONB, so every value is range-checked and silently falls back rather
    than 500ing the public calculator."""
    raw: dict[str, Any] = tenant.settings.get("mortgage") or {}
    rate = DEFAULT_ANNUAL_RATE_PERCENT
    value = raw.get("default_annual_rate_percent")
    if isinstance(value, int | float) and 0 <= value <= 100:
        rate = Decimal(str(value))
    term = raw.get("default_term_years")
    term_years = term if isinstance(term, int) and 1 <= term <= 40 else DEFAULT_TERM_YEARS
    down = DEFAULT_DOWN_PAYMENT_PERCENT
    value = raw.get("default_down_payment_percent")
    if isinstance(value, int | float) and 0 <= value <= 95:
        down = Decimal(str(value))
    return rate, term_years, down


def _band(ppsm: list[float], area: float) -> tuple[Decimal, Decimal]:
    """Interquartile price/m2 times area. Float math is fine here — the output
    is a coarse band, quantized back to money at the end."""
    values = sorted(ppsm)

    def percentile(q: float) -> float:
        pos = (len(values) - 1) * q
        lower = int(pos)
        upper = min(lower + 1, len(values) - 1)
        return values[lower] + (values[upper] - values[lower]) * (pos - lower)

    low = Decimal(str(percentile(0.25) * area)).quantize(_CENT, rounding=ROUND_HALF_UP)
    high = Decimal(str(percentile(0.75) * area)).quantize(_CENT, rounding=ROUND_HALF_UP)
    return low, high


class ValuationsService:
    def __init__(
        self,
        repo: ValuationsRepository,
        leads: LeadsService,
        listings: ListingService,
        settings: Settings,
    ) -> None:
        self.repo = repo
        self.leads = leads
        self.listings = listings
        self.settings = settings

    # ---- the step flow (public) ----

    async def start(
        self, tenant: TenantContext, data: ValuationStartCreate
    ) -> tuple[ValuationRequest, str]:
        row = ValuationRequest(
            tenant_id=tenant.id,
            address={
                k: v
                for k, v in {
                    "street": data.street,
                    "city": data.city,
                    "postal_code": data.postal_code,
                }.items()
                if v is not None
            },
            location=to_point(data.lat, data.lng)
            if data.lat is not None and data.lng is not None
            else None,
        )
        self.repo.add(row)
        await self.repo.flush()
        token = sign_value(_TOKEN_PURPOSE, f"{tenant.id}:{row.id}", self.settings)
        return row, token

    async def _resolve(self, tenant: TenantContext, token: str) -> ValuationRequest:
        """Token → row, or 404 — forged signatures, foreign-tenant tokens and
        unknown ids are indistinguishable to the caller (no oracle)."""
        value = unsign_value(_TOKEN_PURPOSE, token, self.settings)
        if value is None:
            raise NotFoundError("Valuation request not found.")
        tenant_part, sep, id_part = value.partition(":")
        if not sep or tenant_part != str(tenant.id):
            raise NotFoundError("Valuation request not found.")
        try:
            request_id = uuid.UUID(id_part)
        except ValueError:
            raise NotFoundError("Valuation request not found.") from None
        row = await self.repo.get(tenant.id, request_id)
        if row is None:
            raise NotFoundError("Valuation request not found.")
        return row

    async def update_details(
        self, tenant: TenantContext, token: str, data: ValuationDetailsUpdate
    ) -> ValuationRequest:
        row = await self._resolve(tenant, token)
        if row.completed_at is not None:
            # A completed form is gone from the flow's perspective.
            raise NotFoundError("Valuation request not found.")
        fields = data.model_dump(exclude_unset=True)
        extras = {k: fields.pop(k, None) for k in ("condition", "notes")}
        for key, value in fields.items():
            setattr(row, key, value)
        details = dict(row.details)
        details.update({k: v for k, v in extras.items() if v is not None})
        row.details = details
        return row

    async def complete(
        self, tenant: TenantContext, token: str, data: ValuationCompleteCreate
    ) -> ValuationRequest | None:
        """Honeypot hits return ``None`` — the router synthesizes a null-band
        response indistinguishable from a low-data area, and nothing persists.
        The real path stores the band, mints the CRM lead and stamps
        ``completed_at`` (a second complete is a 409)."""
        if data.hp:
            logger.info("valuation_honeypot_triggered")
            return None

        row = await self._resolve(tenant, token)
        if row.completed_at is not None:
            raise ConflictError("This valuation request was already completed.")

        low, high, comps_count = await self._estimate(tenant, row)
        row.estimate_low = low
        row.estimate_high = high
        row.comps_count = comps_count

        lead = await self.leads.register_valuation_lead(
            tenant,
            data.contact,
            message=data.message,
            source_meta=_capture_source_meta(data),
            property_payload=self._property_payload(row),
        )
        row.contact_id = lead.contact_id
        row.lead_id = lead.id
        row.completed_at = datetime.now(UTC)
        return row

    async def _estimate(
        self, tenant: TenantContext, row: ValuationRequest
    ) -> tuple[Decimal | None, Decimal | None, int]:
        """(low, high, comps_count) — null band when the request lacks a
        point, a type or an area, or when no radius rung reaches MIN_COMPS."""
        lonlat = point_lonlat(row.location)
        if lonlat is None or row.property_type is None or row.area_built is None:
            return None, None, 0
        lon, lat = lonlat
        for radius_km in RADIUS_LADDER_KM:
            comps = await self.listings.comps_for(
                tenant.id,
                lon=lon,
                lat=lat,
                property_type=row.property_type,
                radius_km=radius_km,
                limit=COMPS_LIMIT,
            )
            if len(comps) >= MIN_COMPS:
                ppsm = [float(price) / float(area) for price, area in comps]
                low, high = _band(ppsm, float(row.area_built))
                return low, high, len(comps)
        return None, None, 0

    def _property_payload(self, row: ValuationRequest) -> dict[str, Any]:
        """What the agent sees on the lead timeline: the seller's description
        plus the band the visitor was shown."""
        payload: dict[str, Any] = {
            "address": row.address,
            "property_type": row.property_type.value if row.property_type else None,
            "area_built": str(row.area_built) if row.area_built is not None else None,
            "beds": row.beds,
            "baths": row.baths,
            "floor": row.floor,
            "year_built": row.year_built,
            "estimate_low": str(row.estimate_low) if row.estimate_low is not None else None,
            "estimate_high": str(row.estimate_high) if row.estimate_high is not None else None,
            "currency": row.currency,
            "comps_count": row.comps_count,
        }
        payload.update(row.details)
        return {k: v for k, v in payload.items() if v is not None}

    # ---- mortgage calculator (§8.8) ----

    def mortgage_estimate(
        self, tenant: TenantContext, data: MortgageEstimateIn
    ) -> MortgageEstimateOut:
        rate, term_years, down_percent = _tenant_mortgage_settings(tenant)
        annual_rate = data.annual_rate_percent if data.annual_rate_percent is not None else rate
        years = data.term_years if data.term_years is not None else term_years
        down = (
            data.down_payment
            if data.down_payment is not None
            else (data.price * down_percent / 100).quantize(_CENT, rounding=ROUND_HALF_UP)
        )
        principal = data.price - down
        months = years * 12
        if annual_rate == 0:
            monthly = principal / months
        else:
            monthly_rate = annual_rate / Decimal(100) / Decimal(12)
            factor = (1 + monthly_rate) ** months
            monthly = principal * monthly_rate * factor / (factor - 1)
        monthly = monthly.quantize(_CENT, rounding=ROUND_HALF_UP)
        total_paid = (monthly * months).quantize(_CENT, rounding=ROUND_HALF_UP)
        return MortgageEstimateOut(
            price=data.price,
            down_payment=down,
            loan_amount=principal,
            annual_rate_percent=annual_rate,
            term_years=years,
            monthly_payment=monthly,
            total_paid=total_paid,
            total_interest=total_paid - principal,
        )

    async def mortgage_email(
        self, tenant: TenantContext, data: MortgageEmailCreate
    ) -> tuple[Lead | None, MortgageEstimateOut]:
        """"Email me this estimate": recompute server-side (never trust a
        client-echoed number), mint the lead, and mail post-commit. Honeypot
        hits return the estimate with no lead — the calculator output gives a
        bot nothing to distinguish."""
        estimate = self.mortgage_estimate(tenant, data)
        if data.hp:
            logger.info("mortgage_email_honeypot_triggered")
            return None, estimate

        lead = await self.leads.register_mortgage_lead(
            tenant,
            data.contact,
            listing_id=data.listing_id,
            source_meta=_capture_source_meta(data),
            estimate_payload={
                "price": str(estimate.price),
                "down_payment": str(estimate.down_payment),
                "annual_rate_percent": str(estimate.annual_rate_percent),
                "term_years": estimate.term_years,
                "monthly_payment": str(estimate.monthly_payment),
            },
        )

        email = data.contact.email
        assert email is not None  # schema-validated
        body = (
            "Here is your mortgage estimate:\n\n"
            f"Property price: {estimate.price}\n"
            f"Down payment: {estimate.down_payment}\n"
            f"Loan amount: {estimate.loan_amount}\n"
            f"Rate: {estimate.annual_rate_percent}% over {estimate.term_years} years\n"
            f"Monthly payment: {estimate.monthly_payment}\n"
            f"Total interest: {estimate.total_interest}\n\n"
            "This is an estimate, not a financing offer. An agent can help "
            "you refine it."
        )

        async def _send() -> None:
            send_email.delay(to=email, subject="Your mortgage estimate", text=body)

        # Post-commit: a rolled-back capture must not email anyone.
        on_commit(self.repo.session, _send)
        return lead, estimate


def _capture_source_meta(data: ValuationCompleteCreate | MortgageEmailCreate) -> dict[str, Any]:
    return {
        k: v
        for k, v in {
            "utm_source": data.utm_source,
            "utm_medium": data.utm_medium,
            "utm_campaign": data.utm_campaign,
            "page": data.page,
            "referrer": data.referrer,
        }.items()
        if v is not None
    }


def get_valuations_service(session: SessionDep, request: Request) -> ValuationsService:
    return ValuationsService(
        ValuationsRepository(session),
        get_leads_service(session),
        get_listing_service(session),
        request.app.state.settings,
    )


ValuationsServiceDep = Annotated[ValuationsService, Depends(get_valuations_service)]
