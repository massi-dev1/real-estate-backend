"""Content CMS schemas (§8.10). Two output shapes, as elsewhere (§8.1): the
portal sees the full i18n objects; the public site gets one negotiated locale
per i18n field (resolved server-side via ``pick_localized``).

Block JSON is validated at the *envelope* level only — a known ``type`` and a
``data`` object — because the frontend owns each block's inner schema.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, field_validator

from app.core.i18n import SUPPORTED_LOCALES, pick_localized
from app.core.schema import InputSchema, OutSchema, reject_null_for
from app.modules.content.models import LegalKind, PageStatus

I18nText = dict[str, str]

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
