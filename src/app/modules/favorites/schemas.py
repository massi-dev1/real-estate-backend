"""Pydantic schemas for favorites & saved searches (§8.9).

``filters`` round-trips through ``PublicListingFilters`` at every write —
what lands in the JSONB column is the *validated* camelCase dump, so replaying
it against the public search can't fail later. The anonymous signup carries
the same honeypot/rendered-at spam defense as lead capture (§10.8).
"""

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from app.core.i18n import SUPPORTED_LOCALES
from app.core.schema import InputSchema, OutSchema, reject_null_for
from app.modules.favorites.models import AlertFrequency, SavedSearch
from app.modules.leads.schemas import MAX_FORM_AGE_SECONDS, MIN_FILL_SECONDS
from app.modules.listings.schemas import PublicListingFilters, PublicListingOut


class FavoriteItemOut(OutSchema):
    """One dashboard card: the public listing shape plus when it was saved."""

    favorited_at: datetime
    listing: PublicListingOut


def _validate_locale(value: str | None) -> str | None:
    if value is not None and value not in SUPPORTED_LOCALES:
        raise ValueError(f"unsupported locale (use one of {sorted(SUPPORTED_LOCALES)})")
    return value


def dump_filters(filters: PublicListingFilters) -> dict[str, Any]:
    """The canonical stored form: validated, camelCase, no nulls."""
    return filters.model_dump(mode="json", by_alias=True, exclude_none=True)


class SavedSearchCreate(InputSchema):
    name: str = Field(min_length=1, max_length=120)
    filters: PublicListingFilters = Field(default_factory=PublicListingFilters)
    frequency: AlertFrequency = AlertFrequency.INSTANT
    # Optional override; otherwise the router negotiates from Accept-Language.
    locale: str | None = None

    @field_validator("locale")
    @classmethod
    def known_locale(cls, value: str | None) -> str | None:
        return _validate_locale(value)


class SavedSearchUpdate(InputSchema):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    filters: PublicListingFilters | None = None
    frequency: AlertFrequency | None = None
    is_active: bool | None = None
    locale: str | None = None

    @field_validator("locale")
    @classmethod
    def known_locale(cls, value: str | None) -> str | None:
        return _validate_locale(value)

    _reject_required_nulls = reject_null_for(
        "name", "filters", "frequency", "is_active", "locale"
    )


class SavedSearchOut(OutSchema):
    id: uuid.UUID
    name: str
    filters: dict[str, Any]
    frequency: AlertFrequency
    locale: str
    is_active: bool
    last_run_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: SavedSearch) -> "SavedSearchOut":
        return cls.model_validate(row)


class SavedSearchSignupIn(InputSchema):
    """Anonymous email-only signup (§8.9) — same spam defense as lead capture
    (see leads/schemas.py::LeadCaptureCreate for the hp/rendered_at design)."""

    email: str = Field(max_length=320)
    name: str = Field(default="My search", min_length=1, max_length=120)
    filters: PublicListingFilters = Field(default_factory=PublicListingFilters)
    frequency: AlertFrequency = AlertFrequency.DAILY
    locale: str | None = None
    hp: str = ""
    rendered_at: datetime

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value:
            raise ValueError("a valid email address is required")
        return value

    @field_validator("locale")
    @classmethod
    def known_locale(cls, value: str | None) -> str | None:
        return _validate_locale(value)

    @model_validator(mode="after")
    def not_too_fast(self) -> Self:
        now = datetime.now(self.rendered_at.tzinfo) if self.rendered_at.tzinfo else datetime.now()
        elapsed = (now - self.rendered_at).total_seconds()
        if elapsed < MIN_FILL_SECONDS:
            raise ValueError("form submitted too quickly")
        if elapsed > MAX_FORM_AGE_SECONDS:
            raise ValueError("form is stale — please reload and resubmit")
        return self


class SavedSearchSignupOut(OutSchema):
    """Deliberately minimal — an unauthenticated caller learns only that a
    confirmation email is on its way (the id is fake on honeypot hits)."""

    id: uuid.UUID


class SavedSearchConfirmIn(InputSchema):
    token: str = Field(min_length=1, max_length=200)


class SavedSearchUnsubscribeIn(InputSchema):
    token: str = Field(min_length=1, max_length=200)
