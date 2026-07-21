"""Blog schemas (§8.10). Two output shapes, as elsewhere (§8.1): the portal
sees the full i18n objects; the public site gets one negotiated locale per
i18n field (resolved server-side via ``pick_localized``).

Body/excerpt are sanitized in the **service** layer at write time (nh3), not
here — the allowlist is a business rule, and keeping it out of the schema lets
it be unit-tested independently.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import Field, field_validator, model_validator

from app.core.i18n import SUPPORTED_LOCALES, pick_localized
from app.core.schema import InputSchema, OutSchema, reject_null_for
from app.modules.blog.models import BlogPostStatus
from app.modules.content.schemas import SeoMeta  # schema-to-schema reuse (§5-safe)

I18nText = dict[str, str]

MAX_TAGS = 20
MAX_TAG_LENGTH = 40


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


def _normalize_tags(value: list[str]) -> list[str]:
    """Lowercase, strip, drop empties, dedupe (order-preserving), cap length."""
    seen: dict[str, None] = {}
    for raw in value:
        tag = raw.strip().lower()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f"tag exceeds {MAX_TAG_LENGTH} characters: {tag!r}")
        seen.setdefault(tag, None)
    return list(seen)


# Applied independently per field — never a single shared Field() instance
# (a shared FieldInfo carries no default and would force the field required).
Slug = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")]
TagList = Annotated[list[str], Field(max_length=MAX_TAGS)]


# ---- categories ----


class CategoryCreate(InputSchema):
    slug: Slug
    name: I18nText

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: I18nText) -> I18nText:
        result = _validate_i18n(value, max_length=120, require_content=True)
        assert result is not None
        return result


class CategoryUpdate(InputSchema):
    slug: Slug | None = None
    name: I18nText | None = None

    _reject_required_nulls = reject_null_for("slug")

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=120, require_content=True)


class CategoryOut(OutSchema):
    id: uuid.UUID
    slug: str
    name: I18nText
    created_at: datetime
    updated_at: datetime


class PublicCategoryOut(OutSchema):
    slug: str
    name: str | None

    @classmethod
    def from_category(cls, category: Any, locale: str) -> "PublicCategoryOut":
        return cls(slug=category.slug, name=pick_localized(category.name, locale))


# ---- posts ----


class PostCreate(InputSchema):
    slug: Slug
    title: I18nText
    excerpt: I18nText | None = None
    body: I18nText
    category_id: uuid.UUID | None = None
    tags: TagList = Field(default_factory=list)
    cover_image: str | None = Field(default=None, max_length=500)
    seo_meta: SeoMeta | None = None
    status: BlogPostStatus = BlogPostStatus.DRAFT
    scheduled_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: I18nText) -> I18nText:
        result = _validate_i18n(value, max_length=200, require_content=True)
        assert result is not None
        return result

    @field_validator("excerpt")
    @classmethod
    def valid_excerpt(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=1000, require_content=False)

    @field_validator("body")
    @classmethod
    def valid_body(cls, value: I18nText) -> I18nText:
        result = _validate_i18n(value, max_length=200_000, require_content=True)
        assert result is not None
        return result

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, value: list[str]) -> list[str]:
        return _normalize_tags(value)

    @model_validator(mode="after")
    def scheduled_needs_future_time(self) -> "PostCreate":
        _check_scheduled(self.status, self.scheduled_at)
        return self


class PostUpdate(InputSchema):
    slug: Slug | None = None
    title: I18nText | None = None
    excerpt: I18nText | None = None
    body: I18nText | None = None
    category_id: uuid.UUID | None = None
    tags: TagList | None = None
    cover_image: str | None = Field(default=None, max_length=500)
    seo_meta: SeoMeta | None = None
    status: BlogPostStatus | None = None
    scheduled_at: datetime | None = None

    _reject_required_nulls = reject_null_for("slug", "title", "body", "status")

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=200, require_content=True)

    @field_validator("excerpt")
    @classmethod
    def valid_excerpt(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=1000, require_content=False)

    @field_validator("body")
    @classmethod
    def valid_body(cls, value: I18nText | None) -> I18nText | None:
        return _validate_i18n(value, max_length=200_000, require_content=True)

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_tags(value) if value is not None else None

    @model_validator(mode="after")
    def scheduled_needs_future_time(self) -> "PostUpdate":
        # Only validate when the patch is actually setting SCHEDULED — an
        # unrelated field patch on an already-scheduled post shouldn't require
        # re-supplying a future scheduled_at (the service re-checks state).
        if self.status == BlogPostStatus.SCHEDULED:
            _check_scheduled(self.status, self.scheduled_at)
        return self


def _check_scheduled(status: BlogPostStatus, scheduled_at: datetime | None) -> None:
    if status == BlogPostStatus.SCHEDULED:
        if scheduled_at is None:
            raise ValueError("scheduledAt is required when status is 'scheduled'")
        if scheduled_at <= datetime.now(UTC):
            raise ValueError("scheduledAt must be in the future")


class PostOut(OutSchema):
    """Portal view — full i18n objects."""

    id: uuid.UUID
    slug: str
    title: I18nText
    excerpt: I18nText | None
    body: I18nText
    category_id: uuid.UUID | None
    tags: list[str]
    cover_image: str | None
    seo_meta: dict[str, Any]
    status: BlogPostStatus
    scheduled_at: datetime | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PublicPostOut(OutSchema):
    """Public view — one negotiated locale per i18n field."""

    slug: str
    title: str | None
    excerpt: str | None
    body: str | None
    tags: list[str]
    cover_image: str | None
    category: PublicCategoryOut | None
    published_at: datetime | None
    seo_title: str | None
    seo_description: str | None
    og_image: str | None

    @classmethod
    def from_post(
        cls, post: Any, locale: str, *, excerpt_fallback: str | None = None
    ) -> "PublicPostOut":
        seo = post.seo_meta or {}
        category = getattr(post, "category", None)
        return cls(
            slug=post.slug,
            title=pick_localized(post.title, locale),
            excerpt=pick_localized(post.excerpt, locale) or excerpt_fallback,
            body=pick_localized(post.body, locale),
            tags=post.tags,
            cover_image=post.cover_image,
            category=(
                PublicCategoryOut.from_category(category, locale) if category is not None else None
            ),
            published_at=post.published_at,
            seo_title=pick_localized(seo.get("title"), locale),
            seo_description=pick_localized(seo.get("description"), locale),
            og_image=seo.get("og_image"),
        )
