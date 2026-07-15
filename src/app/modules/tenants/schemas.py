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
    verified_at: datetime | None
    created_at: datetime


class TenantOut(OutSchema):
    id: uuid.UUID
    name: str
    slug: str
    status: str
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    domains: list[TenantDomainOut]


class SiteConfigOut(OutSchema):
    """Public per-tenant site configuration (§4.4) — no internal fields."""

    name: str
    slug: str
    settings: dict[str, Any]
