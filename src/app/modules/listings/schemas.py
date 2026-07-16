"""Pydantic schemas for the listings module.

Two output shapes (§8.1): the portal sees the full i18n objects, the public
site gets one negotiated locale per field (``title``/``description`` resolved
through the fallback chain).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import Field, field_validator, model_validator

from app.core.i18n import SUPPORTED_LOCALES, pick_localized
from app.core.pagination import MAX_PAGE_SIZE
from app.core.schema import BaseSchema, InputSchema, OutSchema
from app.modules.listings.geo import point_lonlat
from app.modules.listings.models import (
    Listing,
    ListingPurpose,
    ListingStatus,
    PricePeriod,
    PropertyType,
)
from app.modules.media.schemas import PublicMediaOut

# Controlled vocabulary (§8.1): filters stay consistent and the GIN index
# stays useful. Grows deliberately, never via free-text input.
LISTING_FEATURES: frozenset[str] = frozenset(
    {
        "air_conditioning",
        "balcony",
        "basement",
        "elevator",
        "equipped_kitchen",
        "fiber_internet",
        "furnished",
        "garage",
        "garden",
        "heating",
        "mountain_view",
        "parking",
        "pool",
        "sea_view",
        "security",
        "solar_panels",
        "terrace",
        "wheelchair_access",
    }
)

# purpose → required price_period (sale has none).
PURPOSE_PRICE_PERIOD: dict[ListingPurpose, PricePeriod | None] = {
    ListingPurpose.SALE: None,
    ListingPurpose.RENT: PricePeriod.MONTH,
    ListingPurpose.RENT_DAILY: PricePeriod.DAY,
}

I18nText = dict[str, str]


def _validate_i18n(
    value: I18nText | None, *, max_length: int, require_content: bool
) -> I18nText | None:
    if value is None:
        return None
    unknown = set(value) - set(SUPPORTED_LOCALES)
    if unknown:
        raise ValueError(f"unsupported locale keys: {sorted(unknown)}")
    cleaned = {k: v.strip() for k, v in value.items() if v and v.strip()}
    for locale, text in cleaned.items():
        if len(text) > max_length:
            raise ValueError(f"'{locale}' text exceeds {max_length} characters")
    if require_content and not cleaned:
        raise ValueError("at least one locale must have content")
    return cleaned


class AddressIn(InputSchema):
    line1: str | None = Field(default=None, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    postal_code: str | None = Field(default=None, max_length=20)
    country: str | None = Field(default=None, max_length=2)


class PointIn(InputSchema):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class PointOut(OutSchema):
    lat: float
    lng: float


PriceField = Annotated[Decimal, Field(gt=0, le=Decimal("999999999999"), decimal_places=2)]
SmallCount = Annotated[int, Field(ge=0, le=100)]
AreaField = Annotated[Decimal, Field(gt=0, le=Decimal("99999999"), decimal_places=2)]


class ListingCreate(InputSchema):
    purpose: ListingPurpose
    property_type: PropertyType
    title: I18nText
    description: I18nText | None = None
    price: PriceField
    currency: str = Field(default="DZD", pattern="^[A-Z]{3}$")
    negotiable: bool = False
    beds: SmallCount | None = None
    baths: SmallCount | None = None
    area_built: AreaField | None = None
    area_land: AreaField | None = None
    floor: int | None = Field(default=None, ge=-5, le=200)
    floors_total: int | None = Field(default=None, ge=1, le=200)
    year_built: int | None = Field(default=None, ge=1800)
    features: list[str] = Field(default_factory=list, max_length=len(LISTING_FEATURES))
    address: AddressIn | None = None
    location: PointIn | None = None
    agent_id: uuid.UUID | None = None
    expires_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: I18nText) -> I18nText:
        result = _validate_i18n(value, max_length=200, require_content=True)
        assert result is not None
        return result

    @field_validator("description")
    @classmethod
    def valid_description(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=10_000, require_content=False)

    @field_validator("features")
    @classmethod
    def known_features(cls, value: list[str]) -> list[str]:
        unknown = set(value) - LISTING_FEATURES
        if unknown:
            raise ValueError(f"unknown features: {sorted(unknown)}")
        return sorted(set(value))

    @field_validator("year_built")
    @classmethod
    def sane_year(cls, value: int | None) -> int | None:
        if value is not None and value > datetime.now(UTC).year + 5:
            raise ValueError("year_built is too far in the future")
        return value

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.floor is not None
            and self.floors_total is not None
            and self.floor > self.floors_total
        ):
            raise ValueError("floor cannot exceed floors_total")
        return self


# PATCH fields whose column is NOT NULL — an explicit ``null`` cannot mean
# "clear it", so it is rejected instead of dying at the DB.
_NON_NULLABLE_UPDATE_FIELDS = frozenset(
    {"property_type", "title", "description", "price", "currency", "negotiable", "features"}
)


class ListingUpdate(InputSchema):
    """PATCH payload — everything optional, ``exclude_unset`` semantics.

    ``purpose`` is immutable after creation (it anchors the price period and
    the reference's meaning); ``status`` moves only through transitions.
    """

    property_type: PropertyType | None = None
    title: I18nText | None = None
    description: I18nText | None = None
    price: PriceField | None = None
    currency: str | None = Field(default=None, pattern="^[A-Z]{3}$")
    negotiable: bool | None = None
    beds: SmallCount | None = None
    baths: SmallCount | None = None
    area_built: AreaField | None = None
    area_land: AreaField | None = None
    floor: int | None = Field(default=None, ge=-5, le=200)
    floors_total: int | None = Field(default=None, ge=1, le=200)
    year_built: int | None = Field(default=None, ge=1800)
    features: list[str] | None = Field(default=None, max_length=len(LISTING_FEATURES))
    address: AddressIn | None = None
    location: PointIn | None = None
    agent_id: uuid.UUID | None = None
    expires_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=200, require_content=True)

    @field_validator("description")
    @classmethod
    def valid_description(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=10_000, require_content=False)

    @field_validator("features")
    @classmethod
    def known_features(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = set(value) - LISTING_FEATURES
        if unknown:
            raise ValueError(f"unknown features: {sorted(unknown)}")
        return sorted(set(value))

    @model_validator(mode="after")
    def no_explicit_null_for_required(self) -> Self:
        nulled = {
            f
            for f in self.model_fields_set & _NON_NULLABLE_UPDATE_FIELDS
            if getattr(self, f) is None
        }
        if nulled:
            raise ValueError(f"fields cannot be set to null: {sorted(nulled)}")
        return self


class TransitionRequest(InputSchema):
    to_status: ListingStatus


class PublicListingFilters(BaseSchema):
    """Search filters of the public ``GET /listings`` (full search is §8.3)."""

    purpose: ListingPurpose | None = None
    property_type: PropertyType | None = None
    price_min: PriceField | None = None
    price_max: PriceField | None = None
    beds_min: SmallCount | None = None
    baths_min: SmallCount | None = None

    @model_validator(mode="after")
    def sane_price_range(self) -> Self:
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            raise ValueError("priceMin cannot exceed priceMax")
        return self


class PublicListingQuery(PublicListingFilters):
    """The endpoint's full query surface. One model on purpose: FastAPI
    (0.139) drops a query-param model back to a required scalar the moment any
    other ``Query()`` param is declared next to it, so pagination and locale
    live here rather than as separate params.
    """

    cursor: str | None = None
    limit: int | None = Field(default=None, ge=1, le=MAX_PAGE_SIZE)
    locale: str | None = None


def _point_out(value: Any) -> Any:
    """Accept the ORM's WKB/WKT element wherever a ``PointOut`` is expected."""
    if value is None or isinstance(value, PointOut | dict):
        return value
    lonlat = point_lonlat(value)
    return PointOut(lat=lonlat[1], lng=lonlat[0]) if lonlat else None


class ListingOut(OutSchema):
    """Portal shape: full i18n objects, workflow fields included."""

    id: uuid.UUID
    reference_code: str
    agent_id: uuid.UUID | None
    status: ListingStatus
    purpose: ListingPurpose
    property_type: PropertyType
    title: I18nText
    description: I18nText
    price: Decimal
    currency: str
    price_period: PricePeriod | None
    negotiable: bool
    beds: int | None
    baths: int | None
    area_built: Decimal | None
    area_land: Decimal | None
    floor: int | None
    floors_total: int | None
    year_built: int | None
    features: list[str]
    address: dict[str, Any]
    location: PointOut | None
    published_at: datetime | None
    expires_at: datetime | None
    stale_flagged_at: datetime | None
    view_count: int
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @field_validator("location", mode="before")
    @classmethod
    def orm_point(cls, value: Any) -> Any:
        return _point_out(value)


class PublicListingOut(OutSchema):
    """Public shape: one negotiated locale, no workflow internals."""

    id: uuid.UUID
    reference_code: str
    purpose: ListingPurpose
    property_type: PropertyType
    locale: str
    title: str
    description: str | None
    price: Decimal
    currency: str
    price_period: PricePeriod | None
    negotiable: bool
    beds: int | None
    baths: int | None
    area_built: Decimal | None
    area_land: Decimal | None
    floor: int | None
    floors_total: int | None
    year_built: int | None
    features: list[str]
    address: dict[str, Any]
    location: PointOut | None
    published_at: datetime | None
    # Cover photo everywhere; the full gallery only on the detail endpoint
    # (`null` on list responses — the page never needs 50 photos per card).
    cover: PublicMediaOut | None = None
    media: list[PublicMediaOut] | None = None

    @classmethod
    def from_listing(
        cls,
        listing: Listing,
        locale: str,
        *,
        cover: PublicMediaOut | None = None,
        media: list[PublicMediaOut] | None = None,
    ) -> "PublicListingOut":
        lonlat = point_lonlat(listing.location)
        return cls(
            id=listing.id,
            reference_code=listing.reference_code,
            purpose=listing.purpose,
            property_type=listing.property_type,
            locale=locale,
            title=pick_localized(listing.title, locale) or "",
            description=pick_localized(listing.description, locale),
            price=listing.price,
            currency=listing.currency,
            price_period=listing.price_period,
            negotiable=listing.negotiable,
            beds=listing.beds,
            baths=listing.baths,
            area_built=listing.area_built,
            area_land=listing.area_land,
            floor=listing.floor,
            floors_total=listing.floors_total,
            year_built=listing.year_built,
            features=listing.features,
            address=listing.address,
            location=PointOut(lat=lonlat[1], lng=lonlat[0]) if lonlat else None,
            published_at=listing.published_at,
            cover=cover,
            media=media,
        )


class StatusHistoryOut(OutSchema):
    id: uuid.UUID
    from_status: ListingStatus
    to_status: ListingStatus
    changed_by: uuid.UUID | None
    created_at: datetime
