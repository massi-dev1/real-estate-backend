"""Blog business logic (§8.10, slice 2).

Posts carry a draft/scheduled/published lifecycle. Rich-text bodies are
**sanitized at write time** (nh3 allowlist) — the first rich-text surface in
the codebase, per §10's XSS rule ("never trust the frontend will escape it").
Scheduled posts are flipped to published by the Beat sweep in
``workers/tasks/blog.py`` when ``scheduled_at`` is due.

Preview tokens are not needed here (unlike content pages): a blog draft has no
share-before-publish workflow — it is either published to the public feed or
not. Slug conflicts surface as 409 via IntegrityError, mirroring pages.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

import nh3
import structlog
from fastapi import Depends, Request
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.core.database import SessionDep
from app.core.exceptions import ConflictError, NotFoundError
from app.core.i18n import pick_localized
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.tenancy import TenantContext
from app.modules.blog.models import BlogCategory, BlogPost, BlogPostStatus
from app.modules.blog.repository import BlogRepository
from app.modules.blog.schemas import (
    CategoryCreate,
    CategoryUpdate,
    I18nText,
    PostCreate,
    PostUpdate,
)

logger = structlog.get_logger(__name__)

# Same 10k bound as content pages — the combined sitemap stays within the
# 50k-URL cap sitemaps.org mandates (§8.3) without a sitemap index.
BLOG_SITEMAP_MAX_POSTS = 10_000
RSS_MAX_ITEMS = 30
EXCERPT_FALLBACK_CHARS = 200

# Rich-text allowlist (§10). nh3 strips every tag/attribute not listed —
# script/style/on* handlers, javascript: URLs — no denylist needed. `rel` is
# managed by link_rel, so it must NOT appear in the attribute allowlist.
_ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "br",
        "strong",
        "em",
        "b",
        "i",
        "u",
        "ul",
        "ol",
        "li",
        "h2",
        "h3",
        "h4",
        "blockquote",
        "a",
        "img",
    }
)
_ALLOWED_ATTRS: dict[str, set[str]] = {
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "title"},
}
_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})
_LINK_REL = "noopener noreferrer nofollow"


def _sanitize_html(value: str) -> str:
    return nh3.clean(
        value,
        tags=set(_ALLOWED_TAGS),
        attributes={k: set(v) for k, v in _ALLOWED_ATTRS.items()},
        url_schemes=set(_URL_SCHEMES),
        link_rel=_LINK_REL,
    )


def _strip_html(value: str) -> str:
    """Reduce HTML to plain text (no tags allowed) — for excerpt fallback."""
    return nh3.clean(value, tags=set(), attributes={}).strip()


def _sanitize_i18n_html(value: I18nText) -> I18nText:
    return {locale: _sanitize_html(text) for locale, text in value.items()}


def _plaintext_i18n(value: I18nText) -> I18nText:
    return {locale: _strip_html(text) for locale, text in value.items()}


def _excerpt_from_body(body: I18nText, locale: str) -> str | None:
    """Truncated plain-text teaser from the body, for a post with no excerpt.
    Strips tags first so truncation never cuts mid-tag."""
    html = pick_localized(body, locale)
    if not html:
        return None
    text = _strip_html(html)
    if len(text) <= EXCERPT_FALLBACK_CHARS:
        return text
    return text[:EXCERPT_FALLBACK_CHARS].rstrip() + "…"


class BlogService:
    def __init__(self, repo: BlogRepository, settings: Settings) -> None:
        self.repo = repo
        self._settings = settings

    # ---- categories: portal ----

    async def create_category(self, tenant: TenantContext, data: CategoryCreate) -> BlogCategory:
        category = BlogCategory(tenant_id=tenant.id, slug=data.slug, name=data.name)
        self.repo.add(category)
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError("A category with this slug already exists.") from exc
        return category

    async def get_category(self, tenant: TenantContext, category_id: uuid.UUID) -> BlogCategory:
        category = await self.repo.get_category(tenant.id, category_id)
        if category is None:
            raise NotFoundError("Category not found.")
        return category

    async def list_categories(self, tenant: TenantContext) -> list[BlogCategory]:
        return await self.repo.list_categories(tenant.id)

    async def update_category(
        self, tenant: TenantContext, category_id: uuid.UUID, data: CategoryUpdate
    ) -> BlogCategory:
        category = await self.get_category(tenant, category_id)
        for key, value in data.model_dump(exclude_unset=True).items():
            setattr(category, key, value)
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError("A category with this slug already exists.") from exc
        return category

    async def delete_category(self, tenant: TenantContext, category_id: uuid.UUID) -> None:
        category = await self.get_category(tenant, category_id)
        await self.repo.delete_category(category)
        await self.repo.flush()

    # ---- posts: portal ----

    async def create_post(self, tenant: TenantContext, data: PostCreate) -> BlogPost:
        await self._validate_category(tenant, data.category_id)
        published_at = datetime.now(UTC) if data.status == BlogPostStatus.PUBLISHED else None
        post = BlogPost(
            tenant_id=tenant.id,
            slug=data.slug,
            title=data.title,
            excerpt=_plaintext_i18n(data.excerpt) if data.excerpt else None,
            body=_sanitize_i18n_html(data.body),
            category_id=data.category_id,
            tags=data.tags,
            cover_image=data.cover_image,
            seo_meta=data.seo_meta.model_dump(exclude_none=True) if data.seo_meta else {},
            status=data.status,
            scheduled_at=data.scheduled_at if data.status == BlogPostStatus.SCHEDULED else None,
            published_at=published_at,
        )
        self.repo.add(post)
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError("A post with this slug already exists.") from exc
        return post

    async def get_post(self, tenant: TenantContext, post_id: uuid.UUID) -> BlogPost:
        post = await self.repo.get_post(tenant.id, post_id)
        if post is None:
            raise NotFoundError("Post not found.")
        return post

    async def list_posts(
        self,
        tenant: TenantContext,
        *,
        cursor: str | None,
        limit: int | None,
        status: BlogPostStatus | None = None,
        category_id: uuid.UUID | None = None,
    ) -> tuple[list[BlogPost], str | None, int]:
        page_size = clamp_limit(limit)
        after = _decode_keyset(cursor) if cursor else None
        rows = await self.repo.list_posts(
            tenant.id, after=after, limit=page_size, status=status, category_id=category_id
        )
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = encode_cursor(
                {"created_at": last.created_at.isoformat(), "id": str(last.id)}
            )
        total = await self.repo.count_posts(tenant.id)
        return items, next_cursor, total

    async def update_post(
        self, tenant: TenantContext, post_id: uuid.UUID, data: PostUpdate
    ) -> BlogPost:
        post = await self.get_post(tenant, post_id)
        fields = data.model_dump(exclude_unset=True)

        if "category_id" in fields:
            await self._validate_category(tenant, data.category_id)
        if "body" in fields and data.body is not None:
            post.body = _sanitize_i18n_html(data.body)
            fields.pop("body")
        if "excerpt" in fields:
            post.excerpt = _plaintext_i18n(data.excerpt) if data.excerpt else None
            fields.pop("excerpt")
        if "seo_meta" in fields:
            post.seo_meta = data.seo_meta.model_dump(exclude_none=True) if data.seo_meta else {}
            fields.pop("seo_meta")

        status_before = post.status
        for key, value in fields.items():
            setattr(post, key, value)

        # A SCHEDULED post that isn't re-scheduling keeps its scheduled_at; a
        # move away from SCHEDULED clears it (a draft/published post shouldn't
        # carry a stale go-live time the sweep could act on).
        if post.status != BlogPostStatus.SCHEDULED:
            post.scheduled_at = None
        else:
            # Validate the *resulting* state: a partial PATCH (e.g. only
            # scheduledAt, keeping an already-SCHEDULED status) never reaches
            # the schema's future-time validator, which sees only the request
            # fields. Without this, a past scheduled_at would be persisted and
            # the sweep would publish immediately.
            if post.scheduled_at is None:
                raise ConflictError("A scheduled post requires a go-live time.")
            if post.scheduled_at <= datetime.now(UTC):
                raise ConflictError("The scheduled go-live time must be in the future.")
        # First transition into published stamps published_at, once.
        if (
            post.status == BlogPostStatus.PUBLISHED
            and status_before != BlogPostStatus.PUBLISHED
            and post.published_at is None
        ):
            post.published_at = datetime.now(UTC)
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError("A post with this slug already exists.") from exc
        return post

    async def publish_post(self, tenant: TenantContext, post_id: uuid.UUID) -> BlogPost:
        post = await self.get_post(tenant, post_id)
        if post.status != BlogPostStatus.PUBLISHED:
            post.status = BlogPostStatus.PUBLISHED
            post.scheduled_at = None
            if post.published_at is None:
                post.published_at = datetime.now(UTC)
            await self.repo.flush()
        return post

    async def unpublish_post(self, tenant: TenantContext, post_id: uuid.UUID) -> BlogPost:
        post = await self.get_post(tenant, post_id)
        if post.status != BlogPostStatus.DRAFT:
            post.status = BlogPostStatus.DRAFT
            post.scheduled_at = None
            await self.repo.flush()
        return post

    async def delete_post(self, tenant: TenantContext, post_id: uuid.UUID) -> None:
        post = await self.get_post(tenant, post_id)
        await self.repo.delete_post(post)
        await self.repo.flush()

    async def _validate_category(
        self, tenant: TenantContext, category_id: uuid.UUID | None
    ) -> None:
        """A portal-supplied category_id must belong to this tenant — a bad id
        is a 404-shaped user error, not a silent FK IntegrityError 500."""
        if category_id is None:
            return
        if await self.repo.get_category(tenant.id, category_id) is None:
            raise NotFoundError("Category not found.")

    # ---- posts: public ----

    async def get_public_post(self, tenant: TenantContext, slug: str) -> BlogPost:
        post = await self.repo.get_published_post_by_slug(tenant.id, slug)
        if post is None:
            raise NotFoundError("Post not found.")
        return post

    async def list_public_posts(
        self,
        tenant: TenantContext,
        *,
        cursor: str | None,
        limit: int | None,
        category_slug: str | None = None,
        tag: str | None = None,
    ) -> tuple[list[BlogPost], str | None]:
        page_size = clamp_limit(limit)
        after = _decode_public_keyset(cursor) if cursor else None
        rows = await self.repo.list_public_posts(
            tenant.id, after=after, limit=page_size, category_slug=category_slug, tag=tag
        )
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            # published_at is NOT NULL for published rows.
            assert last.published_at is not None
            next_cursor = encode_cursor(
                {"published_at": last.published_at.isoformat(), "id": str(last.id)}
            )
        return items, next_cursor

    def excerpt_fallback(self, post: BlogPost, locale: str) -> str | None:
        return _excerpt_from_body(post.body, locale)

    async def sitemap_posts(self, tenant: TenantContext) -> list[BlogPost]:
        return await self.repo.published_posts_for_sitemap(tenant.id, limit=BLOG_SITEMAP_MAX_POSTS)

    async def rss_feed_posts(self, tenant: TenantContext) -> list[BlogPost]:
        return await self.repo.recent_published_for_rss(tenant.id, limit=RSS_MAX_ITEMS)


def _decode_keyset(cursor: str) -> tuple[datetime, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(values["created_at"]), uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def _decode_public_keyset(cursor: str) -> tuple[datetime, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(values["published_at"]), uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def get_blog_service(session: SessionDep, request: Request) -> BlogService:
    return BlogService(BlogRepository(session), request.app.state.settings)


BlogServiceDep = Annotated[BlogService, Depends(get_blog_service)]
