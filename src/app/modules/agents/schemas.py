"""Pydantic schemas for the agents module (§8.5).

Two output shapes, mirroring listings: the portal sees the full i18n objects
and workflow fields, the public directory gets one negotiated locale and no
internals. ``service_areas`` arrives as JSON rings of ``[lon, lat]`` pairs —
validated and range-checked here, so geometry WKT is only ever built from
parsed floats (same stance as Part 7's ``inPolygon``).
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from app.common.geo import LonLat, multipolygon_rings
from app.core.i18n import SUPPORTED_LOCALES, pick_localized
from app.core.pagination import MAX_PAGE_SIZE
from app.core.schema import BaseSchema, InputSchema, OutSchema
from app.modules.agents.models import AgentProfile, PhotoStatus
from app.modules.listings.models import ListingStatus
from app.modules.listings.schemas import PublicListingOut

I18nText = dict[str, str]

# Controlled vocabulary (like listing features): directory filters stay
# consistent. Grows deliberately, never via free-text input.
AGENT_SPECIALTIES: frozenset[str] = frozenset(
    {
        "residential_sales",
        "residential_rentals",
        "commercial",
        "luxury",
        "land",
        "new_developments",
        "off_plan",
        "property_management",
        "valuation",
        "industrial",
    }
)

# Social keys shown on public profiles; values must be https URLs since the
# frontend renders them as links.
SOCIAL_KEYS: frozenset[str] = frozenset(
    {"facebook", "instagram", "linkedin", "x", "tiktok", "youtube", "website"}
)

MAX_RING_POINTS = 100
MAX_SERVICE_AREAS = 10

PHOTO_CONTENT_TYPES: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp"})

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


def _validate_bio(value: I18nText | None) -> I18nText | None:
    if value is None:
        return None
    unknown = set(value) - set(SUPPORTED_LOCALES)
    if unknown:
        raise ValueError(f"unsupported locale keys: {sorted(unknown)}")
    cleaned = {k: v.strip() for k, v in value.items() if v and v.strip()}
    for locale, text in cleaned.items():
        if len(text) > 5000:
            raise ValueError(f"'{locale}' text exceeds 5000 characters")
    return cleaned


def _validate_specialties(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    unknown = set(value) - AGENT_SPECIALTIES
    if unknown:
        raise ValueError(f"unknown specialties: {sorted(unknown)}")
    return sorted(set(value))


def _validate_socials(value: dict[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    unknown = set(value) - SOCIAL_KEYS
    if unknown:
        raise ValueError(f"unknown social keys: {sorted(unknown)}")
    for key, url in value.items():
        if len(url) > 300:
            raise ValueError(f"'{key}' URL is too long")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(f"'{key}' must be an https URL")
    return value


def _validate_rings(value: list[list[LonLat]] | None) -> list[list[LonLat]] | None:
    """Range-check every coordinate and close each ring (same rules as the
    public ``inPolygon`` filter)."""
    if value is None:
        return None
    if len(value) > MAX_SERVICE_AREAS:
        raise ValueError(f"at most {MAX_SERVICE_AREAS} service areas are supported")
    cleaned: list[list[LonLat]] = []
    for ring in value:
        points = [(float(lon), float(lat)) for lon, lat in ring]
        for lon, lat in points:
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("serviceAreas coordinates out of range (lon then lat)")
        if len(points) > MAX_RING_POINTS:
            raise ValueError(f"each service area supports at most {MAX_RING_POINTS} points")
        if points and points[0] != points[-1]:
            points.append(points[0])  # close the ring
        if len(points) < 4:  # a closed triangle is 4 points
            raise ValueError("each service area needs at least 3 distinct points")
        cleaned.append(points)
    return cleaned


class AgentProfileCreate(InputSchema):
    # Defaults to the caller; managers may create a profile for any agent.
    user_id: uuid.UUID | None = None
    slug: str = Field(min_length=2, max_length=120, pattern=SLUG_PATTERN)
    bio: I18nText | None = None
    specialties: list[str] = Field(default_factory=list, max_length=len(AGENT_SPECIALTIES))
    service_areas: list[list[LonLat]] | None = Field(default=None)
    license_no: str | None = Field(default=None, max_length=100)
    socials: dict[str, str] | None = None

    @field_validator("bio")
    @classmethod
    def valid_bio(cls, value: I18nText | None) -> I18nText | None:
        return _validate_bio(value)

    @field_validator("specialties")
    @classmethod
    def known_specialties(cls, value: list[str]) -> list[str]:
        result = _validate_specialties(value)
        assert result is not None
        return result

    @field_validator("socials")
    @classmethod
    def valid_socials(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _validate_socials(value)

    @field_validator("service_areas")
    @classmethod
    def valid_areas(cls, value: list[list[LonLat]] | None) -> list[list[LonLat]] | None:
        return _validate_rings(value)


class AgentProfileUpdate(InputSchema):
    """PATCH payload — ``exclude_unset`` semantics. ``userId`` is immutable;
    ``isPublished`` is manager-gated in the service (curated directory)."""

    slug: str | None = Field(default=None, min_length=2, max_length=120, pattern=SLUG_PATTERN)
    bio: I18nText | None = None
    specialties: list[str] | None = Field(default=None, max_length=len(AGENT_SPECIALTIES))
    service_areas: list[list[LonLat]] | None = None
    license_no: str | None = Field(default=None, max_length=100)
    socials: dict[str, str] | None = None
    is_published: bool | None = None

    @field_validator("bio")
    @classmethod
    def valid_bio(cls, value: I18nText | None) -> I18nText | None:
        return _validate_bio(value)

    @field_validator("specialties")
    @classmethod
    def known_specialties(cls, value: list[str] | None) -> list[str] | None:
        return _validate_specialties(value)

    @field_validator("socials")
    @classmethod
    def valid_socials(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _validate_socials(value)

    @field_validator("service_areas")
    @classmethod
    def valid_areas(cls, value: list[list[LonLat]] | None) -> list[list[LonLat]] | None:
        return _validate_rings(value)

    @model_validator(mode="after")
    def no_explicit_null_for_required(self) -> Self:
        nulled = {
            f
            for f in self.model_fields_set & {"slug", "specialties", "is_published"}
            if getattr(self, f) is None
        }
        if nulled:
            raise ValueError(f"fields cannot be set to null: {sorted(nulled)}")
        return self


class PhotoVariantOut(OutSchema):
    url: str
    width: int
    height: int


def _photo_variants_out(
    profile: AgentProfile, url_for: Callable[[str], str]
) -> dict[str, PhotoVariantOut]:
    return {
        name: PhotoVariantOut(url=url_for(v["key"]), width=v["width"], height=v["height"])
        for name, v in profile.photo_variants.items()
    }


class AgentProfileOut(OutSchema):
    """Portal shape: full i18n bio, photo pipeline state included."""

    id: uuid.UUID
    user_id: uuid.UUID
    slug: str
    bio: I18nText
    specialties: list[str]
    service_areas: list[list[LonLat]] | None
    license_no: str | None
    socials: dict[str, str]
    is_published: bool
    photo_status: PhotoStatus | None
    photo_variants: dict[str, PhotoVariantOut]
    photo_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_profile(
        cls, profile: AgentProfile, url_for: Callable[[str], str]
    ) -> "AgentProfileOut":
        return cls(
            id=profile.id,
            user_id=profile.user_id,
            slug=profile.slug,
            bio=profile.bio,
            specialties=profile.specialties,
            service_areas=multipolygon_rings(profile.service_areas),
            license_no=profile.license_no,
            socials=profile.socials,
            is_published=profile.is_published,
            photo_status=profile.photo_status,
            photo_variants=_photo_variants_out(profile, url_for),
            photo_error=profile.photo_error,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )


class PublicAgentOut(OutSchema):
    """Directory card: one negotiated locale, no internals, no service areas
    (agency territory maps are back-office data, not public content)."""

    id: uuid.UUID
    slug: str
    display_name: str
    locale: str
    bio: str | None
    specialties: list[str]
    license_no: str | None
    socials: dict[str, str]
    photo_variants: dict[str, PhotoVariantOut]

    @classmethod
    def from_profile(
        cls,
        profile: AgentProfile,
        display_name: str,
        locale: str,
        url_for: Callable[[str], str],
    ) -> "PublicAgentOut":
        return cls(
            id=profile.id,
            slug=profile.slug,
            display_name=display_name,
            locale=locale,
            bio=pick_localized(profile.bio, locale),
            specialties=profile.specialties,
            license_no=profile.license_no,
            socials=profile.socials,
            photo_variants=(
                _photo_variants_out(profile, url_for)
                if profile.photo_status is PhotoStatus.READY
                else {}
            ),
        )


class PublicAgentDetailOut(PublicAgentOut):
    """Profile page: the card plus the agent's active listings."""

    listings: list[PublicListingOut]


class PublicAgentQuery(BaseSchema):
    """Directory query surface — one model like ``PublicListingQuery`` (the
    FastAPI query-model degradation gotcha from Part 4 applies here too)."""

    cursor: str | None = None
    limit: int | None = Field(default=None, ge=1, le=MAX_PAGE_SIZE)
    locale: str | None = None
    specialty: str | None = None

    @field_validator("specialty")
    @classmethod
    def known_specialty(cls, value: str | None) -> str | None:
        if value is not None and value not in AGENT_SPECIALTIES:
            raise ValueError("unknown specialty")
        return value


class TeamCreate(InputSchema):
    name: str = Field(min_length=1, max_length=120)
    lead_user_id: uuid.UUID | None = None


class TeamUpdate(InputSchema):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    lead_user_id: uuid.UUID | None = None


class TeamMemberAdd(InputSchema):
    user_id: uuid.UUID
    role_in_team: str | None = Field(default=None, max_length=40)


class TeamMemberOut(OutSchema):
    user_id: uuid.UUID
    role_in_team: str | None
    created_at: datetime


class TeamOut(OutSchema):
    id: uuid.UUID
    name: str
    lead_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class TeamDetailOut(TeamOut):
    members: list[TeamMemberOut]


class PhotoUploadRequest(InputSchema):
    content_type: str
    size_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def uploadable(self) -> Self:
        if self.content_type not in PHOTO_CONTENT_TYPES:
            raise ValueError(f"contentType must be one of {sorted(PHOTO_CONTENT_TYPES)}")
        return self


class PhotoUploadOut(OutSchema):
    profile: AgentProfileOut
    upload_url: str
    upload_headers: dict[str, str]
    expires_in_seconds: int


class AgentStatsOut(OutSchema):
    """§8.5 performance slice v1. Commission totals (§8.13), tours (§8.7) and
    reviews (§8.11) join when their modules exist."""

    user_id: uuid.UUID
    listings_by_status: dict[ListingStatus, int]
    leads_by_stage: dict[str, int]
    avg_first_response_seconds: float | None
