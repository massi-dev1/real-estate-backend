"""Content CMS schemas (§8.10). Two output shapes, as elsewhere (§8.1): the
portal sees the full i18n objects; the public site gets one negotiated locale
per i18n field (resolved server-side via ``pick_localized``).

Block JSON is validated at the *envelope* level only — a known ``type`` and a
``data`` object — because the frontend owns each block's inner schema.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import Field, field_validator, model_validator

from app.common.geo import LonLat, multipolygon_rings
from app.core.i18n import SUPPORTED_LOCALES, pick_localized
from app.core.schema import InputSchema, OutSchema, reject_null_for
from app.modules.content.models import LegalKind, PageStatus, ReportStatus
from app.modules.leads.schemas import _CaptureBase

I18nText = dict[str, str]

# A guide boundary is at most this many rings, each at most this many points —
# same defensive bounds as agents' service areas (Part 9).
MAX_GUIDE_RINGS = 20
MAX_RING_POINTS = 500

# The block-type vocabulary the frontend renders. New types land here as the
# site grows; an unknown type is rejected so a typo never ships silently.
BLOCK_TYPES: frozenset[str] = frozenset(
    {"hero", "richtext", "listings_grid", "cta", "image", "gallery", "faq", "stats", "contact"}
)


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


class PageBlock(InputSchema):
    """One content block. ``data`` is opaque to the backend — the frontend
    validates and renders it — but the envelope (known type, dict data) is
    checked so garbage never persists."""

    type: str
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def known_type(cls, value: str) -> str:
        if value not in BLOCK_TYPES:
            raise ValueError(f"unknown block type: {value!r}")
        return value


class SeoMeta(InputSchema):
    title: I18nText | None = None
    description: I18nText | None = None
    og_image: str | None = Field(default=None, max_length=500)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=200, require_content=False)

    @field_validator("description")
    @classmethod
    def valid_description(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=400, require_content=False)


Slug = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]


class PageCreate(InputSchema):
    slug: Slug
    title: I18nText
    blocks: list[PageBlock] = Field(default_factory=list)
    seo_meta: SeoMeta | None = None
    status: PageStatus = PageStatus.DRAFT

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: I18nText) -> I18nText:
        result = _validate_i18n(value, max_length=200, require_content=True)
        assert result is not None  # require_content guarantees non-None
        return result


class PageUpdate(InputSchema):
    slug: Slug | None = None
    title: I18nText | None = None
    blocks: list[PageBlock] | None = None
    seo_meta: SeoMeta | None = None
    status: PageStatus | None = None

    _reject_required_nulls = reject_null_for("slug", "status")

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=200, require_content=True)


class PageOut(OutSchema):
    """Portal view — full i18n objects."""

    id: uuid.UUID
    slug: str
    title: I18nText
    blocks: list[dict[str, Any]]
    seo_meta: dict[str, Any]
    status: PageStatus
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PublicPageOut(OutSchema):
    """Public view — one negotiated locale per i18n field."""

    slug: str
    title: str | None
    blocks: list[dict[str, Any]]
    seo_title: str | None
    seo_description: str | None
    og_image: str | None

    @classmethod
    def from_page(cls, page: Any, locale: str) -> "PublicPageOut":
        seo = page.seo_meta or {}
        return cls(
            slug=page.slug,
            title=pick_localized(page.title, locale),
            blocks=page.blocks,
            seo_title=pick_localized(seo.get("title"), locale),
            seo_description=pick_localized(seo.get("description"), locale),
            og_image=seo.get("og_image"),
        )


class PreviewTokenOut(OutSchema):
    token: str


# ---- legal pages ----


class LegalPageCreate(InputSchema):
    kind: LegalKind
    body: I18nText
    effective_at: datetime | None = None

    @field_validator("body")
    @classmethod
    def valid_body(cls, value: I18nText) -> I18nText:
        result = _validate_i18n(value, max_length=100_000, require_content=True)
        assert result is not None
        return result


class LegalPageOut(OutSchema):
    """Portal / history view — full i18n objects."""

    id: uuid.UUID
    kind: LegalKind
    version: int
    body: I18nText
    effective_at: datetime
    is_current: bool
    created_at: datetime


class PublicLegalPageOut(OutSchema):
    """Public view — one negotiated locale."""

    kind: LegalKind
    version: int
    body: str | None
    effective_at: datetime

    @classmethod
    def from_page(cls, page: Any, locale: str) -> "PublicLegalPageOut":
        return cls(
            kind=page.kind,
            version=page.version,
            body=pick_localized(page.body, locale),
            effective_at=page.effective_at,
        )


class LegalIndexEntry(OutSchema):
    """Footer index — kind + title, no body."""

    kind: LegalKind
    version: int
    effective_at: datetime


# ---- neighborhood guides (§8.10 slice 3) ----


def _validate_boundary(value: list[list[LonLat]] | None) -> list[list[LonLat]] | None:
    """Range-check every coordinate and close each ring — same rules as agents'
    service areas and Part 7's ``inPolygon``. Geometry WKT is only ever built
    from these parsed floats, never from raw client text."""
    if value is None:
        return None
    if len(value) > MAX_GUIDE_RINGS:
        raise ValueError(f"at most {MAX_GUIDE_RINGS} boundary rings are supported")
    cleaned: list[list[LonLat]] = []
    for ring in value:
        points = [(float(lon), float(lat)) for lon, lat in ring]
        for lon, lat in points:
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("boundary coordinates out of range (lon then lat)")
        if len(points) > MAX_RING_POINTS:
            raise ValueError(f"each boundary ring supports at most {MAX_RING_POINTS} points")
        if points and points[0] != points[-1]:
            points.append(points[0])  # close the ring
        if len(points) < 4:  # a closed triangle is 4 points
            raise ValueError("each boundary ring needs at least 3 distinct points")
        cleaned.append(points)
    return cleaned


class GuideCreate(InputSchema):
    slug: Slug
    name: I18nText
    body: I18nText | None = None
    # Rings of [lon, lat] pairs (same shape as agents' serviceAreas).
    boundary: list[list[LonLat]] | None = None
    seo_meta: SeoMeta | None = None
    status: PageStatus = PageStatus.DRAFT

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: I18nText) -> I18nText:
        result = _validate_i18n(value, max_length=200, require_content=True)
        assert result is not None
        return result

    @field_validator("body")
    @classmethod
    def valid_body(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=100_000, require_content=False)

    @field_validator("boundary")
    @classmethod
    def valid_boundary(cls, value: list[list[LonLat]] | None) -> list[list[LonLat]] | None:
        return _validate_boundary(value)


class GuideUpdate(InputSchema):
    slug: Slug | None = None
    name: I18nText | None = None
    body: I18nText | None = None
    boundary: list[list[LonLat]] | None = None
    seo_meta: SeoMeta | None = None
    status: PageStatus | None = None

    _reject_required_nulls = reject_null_for("slug", "name", "status")

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=200, require_content=True)

    @field_validator("body")
    @classmethod
    def valid_body(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=100_000, require_content=False)

    @field_validator("boundary")
    @classmethod
    def valid_boundary(cls, value: list[list[LonLat]] | None) -> list[list[LonLat]] | None:
        return _validate_boundary(value)


class GuideOut(OutSchema):
    """Portal view — full i18n objects, boundary rings, computed stats."""

    id: uuid.UUID
    slug: str
    name: I18nText
    body: I18nText
    boundary: list[list[LonLat]] | None
    seo_meta: dict[str, Any]
    status: PageStatus
    stats: dict[str, Any]
    stats_computed_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_validator("boundary", mode="before")
    @classmethod
    def orm_boundary(cls, value: Any) -> Any:
        if value is None or isinstance(value, list):
            return value
        return multipolygon_rings(value)


class PublicGuideOut(OutSchema):
    """Public list/detail view — one negotiated locale, boundary rings, stats.
    ``listings`` is populated on the detail endpoint only."""

    slug: str
    name: str | None
    body: str | None
    boundary: list[list[LonLat]] | None
    seo_title: str | None
    seo_description: str | None
    og_image: str | None
    stats: dict[str, Any]

    @classmethod
    def from_guide(cls, guide: Any, locale: str) -> "PublicGuideOut":
        seo = guide.seo_meta or {}
        return cls(
            slug=guide.slug,
            name=pick_localized(guide.name, locale),
            body=pick_localized(guide.body, locale),
            boundary=multipolygon_rings(guide.boundary),
            seo_title=pick_localized(seo.get("title"), locale),
            seo_description=pick_localized(seo.get("description"), locale),
            og_image=seo.get("og_image"),
            stats=guide.stats or {},
        )


# ---- market reports (§8.10 slice 3) ----


class ReportCreate(InputSchema):
    slug: Slug
    title: I18nText
    # Author-supplied charts/numbers — opaque JSON the frontend renders and
    # the PDF worker lays out; envelope-validated only (a dict), like page
    # blocks, since the agency owns the report's inner structure.
    stats: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: I18nText) -> I18nText:
        result = _validate_i18n(value, max_length=200, require_content=True)
        assert result is not None
        return result


class ReportUpdate(InputSchema):
    slug: Slug | None = None
    title: I18nText | None = None
    stats: dict[str, Any] | None = None

    _reject_required_nulls = reject_null_for("slug", "title", "stats")

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=200, require_content=True)


class ReportOut(OutSchema):
    """Portal view — full i18n title, author stats, PDF state."""

    id: uuid.UUID
    slug: str
    title: I18nText
    stats: dict[str, Any]
    status: ReportStatus
    generated_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PublicReportOut(OutSchema):
    """Public metadata/stats — deliberately no PDF URL (that's gated)."""

    slug: str
    title: str | None
    stats: dict[str, Any]
    published_at: datetime | None
    # False until the worker finishes rendering — the gate 409s otherwise.
    pdf_ready: bool

    @classmethod
    def from_report(cls, report: Any, locale: str) -> "PublicReportOut":
        return cls(
            slug=report.slug,
            title=pick_localized(report.title, locale),
            stats=report.stats or {},
            published_at=report.published_at,
            pdf_ready=report.status == ReportStatus.READY,
        )


class ReportDownloadCreate(_CaptureBase):
    """The download gate (§8.10 "email required to download → lead"). Nothing
    beyond the shared capture shape — the report is addressed by slug in the
    path, the source is fixed server-side. Reuses the same honeypot +
    ``renderedAt`` spam defense as every other public capture surface."""

    @model_validator(mode="after")
    def email_required(self) -> Self:
        # A honeypot hit (hp filled) must reach the router's camouflaged 200,
        # never a distinguishable 422 — so only genuine submissions are held
        # to the "email needed to send the download" rule.
        if not self.hp and not self.contact.email:
            raise ValueError("contact.email is required to download a report")
        return self


class ReportDownloadOut(OutSchema):
    download_url: str
