"""Pydantic schemas for the tenants module (platform API + public site config)."""

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.core.schema import InputSchema, OutSchema

SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
# Hostname: dot-separated labels, no leading/trailing hyphen, at least two labels.
DOMAIN_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def _normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if len(domain) > 253 or not DOMAIN_RE.fullmatch(domain):
        raise ValueError("must be a valid lowercase domain name")
    return domain


class TenantCreate(InputSchema):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=2, max_length=63, pattern=SLUG_PATTERN)
    domain: str = Field(description="Primary domain the agency site is served on.")
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return _normalize_domain(value)


class TenantUpdate(InputSchema):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    settings: dict[str, Any] | None = None


class TenantDomainCreate(InputSchema):
    domain: str
    is_primary: bool = False

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        return _normalize_domain(value)


class TenantDomainOut(OutSchema):
    id: uuid.UUID
    domain: str
    is_primary: bool
    verification_status: str
    verification_token: str | None
    verified_at: datetime | None
    created_at: datetime


class TenantOut(OutSchema):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    plan: str
    trial_ends_at: datetime | None
    offboarding_at: datetime | None
    deletion_scheduled_at: datetime | None
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    domains: list[TenantDomainOut]


class TenantPlanUpdate(InputSchema):
    """Platform-admin plan change (§8.16). Validated against the code-owned
    plans table in the service — an unknown key is a 422."""

    plan: str = Field(min_length=1, max_length=40)


class DomainVerifyOut(OutSchema):
    """Result of a DNS-verification attempt (§8.16)."""

    domain: str
    verification_status: str
    verified_at: datetime | None


class PlanLimitsOut(OutSchema):
    max_listings: int | None
    max_agents: int | None
    storage_gb: int | None
    monthly_emails: int | None


class UsageOut(OutSchema):
    listings_count: int
    agents_count: int
    storage_bytes: int
    emails_sent: int


class SiteConfigOut(OutSchema):
    """Public per-tenant site configuration (§4.4) — no internal fields.

    Part 22 (§8.16) surfaces the plan, current usage and limits so the agency
    dashboard can render "42 / 100 listings used" without a second call.
    """

    name: str
    slug: str
    settings: dict[str, Any]
    plan: str
    usage: UsageOut
    limits: PlanLimitsOut


# ---- billing (§8.16) ----


class SubscriptionOut(OutSchema):
    id: uuid.UUID
    provider: str
    plan: str
    status: str
    current_period_end: datetime | None
    grace_until: datetime | None
    cancel_at_period_end: bool


class CheckoutCreate(InputSchema):
    plan: str = Field(min_length=1, max_length=40)
    customer_email: str = Field(min_length=3, max_length=320)


class CheckoutOut(OutSchema):
    url: str
    session_id: str


class WebhookAck(OutSchema):
    """Every verified webhook acks 200 (§10.9) — the body says what happened so
    a provider dashboard shows a useful result, without leaking internals."""

    received: bool
    processed: bool


# ---- impersonation (§8.16/§10.11) ----


class ImpersonationOut(OutSchema):
    """A time-boxed impersonation session. ``impersonation`` is the explicit
    frontend "banner active" signal — the token also carries an ``imp`` claim,
    but the response says so plainly so the client need not decode the JWT."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    impersonation: bool = True
    tenant_id: uuid.UUID
    tenant_slug: str
    acting_as_user_id: uuid.UUID


# ---- audit log (§10.11) ----


class AuditLogOut(OutSchema):
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    actor_role: str | None
    action: str
    target: str | None
    # The ORM column is ``metadata`` but mapped to the ``audit_metadata``
    # attribute (``metadata`` is reserved on the declarative base); read it via
    # a validation alias so ``model_validate(row)`` still works.
    metadata: dict[str, Any] = Field(validation_alias="audit_metadata")
    ip: str | None
    created_at: datetime


# ---- cross-tenant platform metrics (§8.16) ----


class TenantMetricRow(OutSchema):
    tenant_id: uuid.UUID
    tenant_name: str
    status: str
    plan: str
    listings_count: int
    agents_count: int
    storage_bytes: int


class PlatformMetricsOut(OutSchema):
    total_tenants: int
    active_tenants: int
    trial_tenants: int
    suspended_tenants: int
    total_listings: int
    total_agents: int
    tenants: list[TenantMetricRow]
