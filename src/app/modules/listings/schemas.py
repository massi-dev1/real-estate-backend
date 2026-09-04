"""Pydantic schemas for the listings module.

Two output shapes (§8.1): the portal sees the full i18n objects, the public
site gets one negotiated locale per field (``title``/``description`` resolved
through the fallback chain).
"""

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import Field, PrivateAttr, field_validator, model_validator

from app.common.geo import point_lonlat
from app.core.i18n import SUPPORTED_LOCALES, pick_localized
from app.core.pagination import MAX_PAGE_SIZE
from app.core.schema import BaseSchema, InputSchema, OutSchema, reject_null_for
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
    # Paid placement (§8.3) — the service restricts this to manager roles.
    featured: bool | None = None

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

    # PATCH fields whose column is NOT NULL — an explicit ``null`` cannot mean
    # "clear it", so it is rejected instead of dying at the DB.
    _reject_required_nulls = reject_null_for(
        "property_type",
        "title",
        "description",
        "price",
        "currency",
        "negotiable",
        "features",
        "featured",
    )


class TransitionRequest(InputSchema):
    to_status: ListingStatus


class GenerateDescriptionRequest(InputSchema):
    """`POST /listings/{id}/generate-description` (§8.18). Which locales to draft
    (default: every supported locale). ``tone`` steers the copy; free text so the
    frontend can offer presets without a schema change."""

    locales: list[str] = Field(default_factory=lambda: list(SUPPORTED_LOCALES))
    tone: str | None = Field(default=None, max_length=100)

    @field_validator("locales")
    @classmethod
    def known_locales(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one locale is required")
        unknown = set(value) - set(SUPPORTED_LOCALES)
        if unknown:
            raise ValueError(f"unsupported locale keys: {sorted(unknown)}")
        # Preserve request order, drop dupes.
        return list(dict.fromkeys(value))


class GeneratedDescriptionOut(OutSchema):
    """An AI-drafted i18n description (§8.18) — a **draft** the agent reviews and
    explicitly saves via the normal PATCH; never auto-persisted over their copy.
    ``model`` names the provider/model that produced it (``stub-echo`` offline)."""

    description: I18nText
    model: str


class SearchSort(enum.StrEnum):
    """Public sort options (§8.3). Featured listings lead every sort."""

    NEWEST = "newest"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"
    AREA_ASC = "area_asc"
    AREA_DESC = "area_desc"


class PortalSort(enum.StrEnum):
    """Back-office sort options. Deliberately a separate enum from
    ``SearchSort``: the portal has no ``featured``-leads rule (that is paid
    public placement, meaningless when managing inventory) and it sorts by
    ``updated_at`` — "what did I touch last" is the question an agent asks of
    their own list, which the public site never needs."""

    NEWEST = "newest"
    UPDATED = "updated"
    PRICE_ASC = "price_asc"
    PRICE_DESC = "price_desc"


DEFAULT_NEAR_RADIUS_KM = 5.0
MAX_POLYGON_POINTS = 100


def _parse_floats(raw: str, expected: int, name: str) -> tuple[float, ...]:
    parts = [p.strip() for p in raw.split(",")]
    try:
        values = tuple(float(p) for p in parts)
    except ValueError:
        raise ValueError(f"{name} must be {expected} comma-separated numbers") from None
    if len(values) != expected:
        raise ValueError(f"{name} must be {expected} comma-separated numbers")
    return values


def _check_lonlat(lon: float, lat: float, name: str) -> None:
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError(f"{name} coordinates out of range (lon then lat)")


class PublicListingFilters(BaseSchema):
    """Full public search surface (§8.3): attribute filters, keyword, geo.

    Geo params arrive as compact strings (map URLs stay shareable):
    ``inBbox=minLon,minLat,maxLon,maxLat`` · ``near=lon,lat&radiusKm=5`` ·
    ``inPolygon=lon lat,lon lat,...``. At most one geo mode per request;
    parsed forms are exposed as properties, never re-parsed downstream.
    """

    purpose: ListingPurpose | None = None
    property_type: PropertyType | None = None
    price_min: PriceField | None = None
    price_max: PriceField | None = None
    beds_min: SmallCount | None = None
    baths_min: SmallCount | None = None
    area_min: AreaField | None = None
    city: str | None = Field(default=None, max_length=100)
    features: list[str] = Field(default_factory=list, max_length=len(LISTING_FEATURES))
    q: str | None = Field(default=None, max_length=200)
    in_bbox: str | None = Field(default=None, max_length=120)
    near: str | None = Field(default=None, max_length=60)
    radius_km: float | None = Field(default=None, gt=0, le=100)
    in_polygon: str | None = Field(default=None, max_length=4000)

    _bbox: tuple[float, float, float, float] | None = PrivateAttr(default=None)
    _near_point: tuple[float, float] | None = PrivateAttr(default=None)
    _polygon_wkt: str | None = PrivateAttr(default=None)

    @field_validator("q", "city")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return (value or "").strip() or None

    @field_validator("features")
    @classmethod
    def known_features(cls, value: list[str]) -> list[str]:
        unknown = set(value) - LISTING_FEATURES
        if unknown:
            raise ValueError(f"unknown features: {sorted(unknown)}")
        return sorted(set(value))

    @model_validator(mode="after")
    def sane_price_range(self) -> Self:
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            raise ValueError("priceMin cannot exceed priceMax")
        return self

    @model_validator(mode="after")
    def parse_geo(self) -> Self:
        modes = [m for m in (self.in_bbox, self.near, self.in_polygon) if m is not None]
        if len(modes) > 1:
            raise ValueError("use only one of inBbox, near, inPolygon")
        if self.radius_km is not None and self.near is None:
            raise ValueError("radiusKm requires near")
        if self.in_bbox is not None:
            min_lon, min_lat, max_lon, max_lat = _parse_floats(self.in_bbox, 4, "inBbox")
            _check_lonlat(min_lon, min_lat, "inBbox")
            _check_lonlat(max_lon, max_lat, "inBbox")
            if min_lon >= max_lon or min_lat >= max_lat:
                raise ValueError("inBbox must be minLon,minLat,maxLon,maxLat with min < max")
            self._bbox = (min_lon, min_lat, max_lon, max_lat)
        if self.near is not None:
            lon, lat = _parse_floats(self.near, 2, "near")
            _check_lonlat(lon, lat, "near")
            self._near_point = (lon, lat)
        if self.in_polygon is not None:
            points: list[tuple[float, float]] = []
            for pair in self.in_polygon.split(","):
                coords = pair.strip().split()
                try:
                    lon, lat = (float(c) for c in coords)
                except ValueError:
                    raise ValueError(
                        "inPolygon must be 'lon lat' pairs separated by commas"
                    ) from None
                _check_lonlat(lon, lat, "inPolygon")
                points.append((lon, lat))
            if len(points) > MAX_POLYGON_POINTS:
                raise ValueError(f"inPolygon supports at most {MAX_POLYGON_POINTS} points")
            if points and points[0] != points[-1]:
                points.append(points[0])  # close the ring
            if len(points) < 4:  # a closed triangle is 4 points
                raise ValueError("inPolygon needs at least 3 distinct points")
            ring = ", ".join(f"{lon} {lat}" for lon, lat in points)
            self._polygon_wkt = f"POLYGON(({ring}))"
        return self

    @property
    def bbox(self) -> tuple[float, float, float, float] | None:
        """(min_lon, min_lat, max_lon, max_lat) once validated."""
        return self._bbox

    @property
    def near_point(self) -> tuple[float, float] | None:
        """(lon, lat) once validated."""
        return self._near_point

    @property
    def near_radius_km(self) -> float:
        return self.radius_km if self.radius_km is not None else DEFAULT_NEAR_RADIUS_KM

    @property
    def polygon_wkt(self) -> str | None:
        return self._polygon_wkt


class PublicListingQuery(PublicListingFilters):
    """The endpoint's full query surface. One model on purpose: FastAPI
    (0.139) drops a query-param model back to a required scalar the moment any
    other ``Query()`` param is declared next to it, so pagination, sort and
    locale live here rather than as separate params.
    """

    sort: SearchSort = SearchSort.NEWEST
    cursor: str | None = None
    limit: int | None = Field(default=None, ge=1, le=MAX_PAGE_SIZE)
    locale: str | None = None


class MapQuery(PublicListingFilters):
    """`GET /listings/map` — the same filters, no pagination (the viewport is
    the page)."""

    locale: str | None = None


class MapPinOut(OutSchema):
    """One dot on the map (§8.3) — deliberately tiny."""

    id: uuid.UUID
    lat: float
    lng: float
    price: Decimal
    status: ListingStatus


class MapClusterOut(OutSchema):
    """Server-side cluster: centroid + count, one per geohash bucket."""

    lat: float
    lng: float
    count: int


class MapOut(OutSchema):
    clustered: bool
    pins: list[MapPinOut]
    clusters: list[MapClusterOut]


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
    featured: bool
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
    featured: bool
    # Cover photo everywhere; the full gallery only on the detail endpoint
    # (`null` on list responses — the page never needs 50 photos per card).
    cover: PublicMediaOut | None = None
    media: list[PublicMediaOut] | None = None
    # JSON-LD structured data (§8.3) — detail responses only; the frontend
    # embeds it verbatim in a <script type="application/ld+json">.
    json_ld: dict[str, Any] | None = None

    @classmethod
    def from_listing(
        cls,
        listing: Listing,
        locale: str,
        *,
        cover: PublicMediaOut | None = None,
        media: list[PublicMediaOut] | None = None,
        json_ld: dict[str, Any] | None = None,
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
            featured=listing.featured,
            cover=cover,
            media=media,
            json_ld=json_ld,
        )


def build_json_ld(listing: Listing, locale: str, *, images: list[str]) -> dict[str, Any]:
    """schema.org ``RealEstateListing`` for the public detail page (§8.3).

    JSON-LD-*ready*: the frontend owns the page URL, so no ``url`` key here.
    """
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "RealEstateListing",
        "name": pick_localized(listing.title, locale) or "",
        "identifier": listing.reference_code,
        "offers": {
            "@type": "Offer",
            "price": str(listing.price),
            "priceCurrency": listing.currency,
        },
    }
    description = pick_localized(listing.description, locale)
    if description:
        data["description"] = description
    if listing.published_at is not None:
        data["datePosted"] = listing.published_at.isoformat()
    if images:
        data["image"] = images
    if listing.beds is not None:
        data["numberOfRooms"] = listing.beds
    if listing.area_built is not None:
        data["floorSize"] = {
            "@type": "QuantitativeValue",
            "value": str(listing.area_built),
            "unitCode": "MTK",  # UN/CEFACT: square metres
        }
    address = {
        key: listing.address[src]
        for key, src in (
            ("streetAddress", "line1"),
            ("addressLocality", "city"),
            ("addressRegion", "state"),
            ("postalCode", "postal_code"),
            ("addressCountry", "country"),
        )
        if listing.address.get(src)
    }
    if address:
        data["address"] = {"@type": "PostalAddress", **address}
    lonlat = point_lonlat(listing.location)
    if lonlat:
        data["geo"] = {"@type": "GeoCoordinates", "latitude": lonlat[1], "longitude": lonlat[0]}
    return data


class StatusHistoryOut(OutSchema):
    id: uuid.UUID
    from_status: ListingStatus
    to_status: ListingStatus
    changed_by: uuid.UUID | None
    created_at: datetime
