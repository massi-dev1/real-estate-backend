"""HTTP layer for the blog (§8.10, slice 2).

- ``public_router`` — the agency site: published posts (paginated, filterable
  by category/tag, one negotiated locale), category list, and an RSS feed.
- ``portal_router`` — the back-office: category + post CRUD with a
  draft/scheduled/published lifecycle. Gated by ``CONTENT_MANAGE`` (the same
  "marketing owns site content" permission pages/legal use).
"""

import uuid
from email.utils import format_datetime
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from app.core.i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    negotiate_locale,
    pick_localized,
)
from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import AuthenticatedUser, Permission, require
from app.core.tenancy import TenantDep
from app.modules.blog.models import BlogPostStatus
from app.modules.blog.schemas import (
    CategoryCreate,
    CategoryOut,
    CategoryUpdate,
    PostCreate,
    PostOut,
    PostUpdate,
    PublicCategoryOut,
    PublicPostOut,
)
from app.modules.blog.service import BlogServiceDep

public_router = APIRouter(tags=["blog:public"])


@public_router.get("/blog/categories")
async def list_public_categories(
    tenant: TenantDep,
    service: BlogServiceDep,
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> list[PublicCategoryOut]:
    resolved = negotiate_locale(locale, accept_language)
    rows = await service.list_categories(tenant)
    return [PublicCategoryOut.from_category(r, resolved) for r in rows]


@public_router.get("/blog/posts")
async def list_public_posts(
    tenant: TenantDep,
    service: BlogServiceDep,
    category: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> Page[PublicPostOut]:
    resolved = negotiate_locale(locale, accept_language)
    items, next_cursor = await service.list_public_posts(
        tenant, cursor=cursor, limit=limit, category_slug=category, tag=tag
    )
    return Page(
        items=[
            PublicPostOut.from_post(
                p, resolved, excerpt_fallback=service.excerpt_fallback(p, resolved)
            )
            for p in items
        ],
        next_cursor=next_cursor,
        total_estimate=None,
    )


@public_router.get("/blog/posts/{slug}")
async def get_public_post(
    slug: str,
    tenant: TenantDep,
    service: BlogServiceDep,
    locale: str | None = Query(default=None),
    accept_language: str | None = Header(default=None),
) -> PublicPostOut:
    resolved = negotiate_locale(locale, accept_language)
    post = await service.get_public_post(tenant, slug)
    return PublicPostOut.from_post(
        post, resolved, excerpt_fallback=service.excerpt_fallback(post, resolved)
    )


@public_router.get("/blog/rss.xml")
async def blog_rss(
    request: Request,
    tenant: TenantDep,
    service: BlogServiceDep,
    locale: str | None = Query(default=None),
) -> Response:
    """Per-tenant RSS 2.0 feed on the request's host. Single-language: RSS
    clients don't send Accept-Language, so ``?locale=`` picks the language,
    else the default locale."""
    resolved = locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE
    posts = await service.rss_feed_posts(tenant)
    host = request.headers.get("host", "").split(":")[0]
    channel_title = escape(tenant.name)
    items = ""
    for post in posts:
        title = escape(pick_localized(post.title, resolved) or post.slug)
        link = f"https://{host}/blog/{escape(post.slug)}"
        description = escape(
            pick_localized(post.excerpt, resolved) or service.excerpt_fallback(post, resolved) or ""
        )
        # RFC-822 pubDate — email.utils, not isoformat (which is wrong for RSS).
        pub_date = format_datetime(post.published_at) if post.published_at is not None else ""
        items += (
            "<item>"
            f"<title>{title}</title>"
            f"<link>{link}</link>"
            f'<guid isPermaLink="true">{link}</guid>'
            f"<description>{description}</description>"
            f"<pubDate>{pub_date}</pubDate>"
            "</item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        f"<title>{channel_title}</title>"
        f"<link>https://{host}/blog</link>"
        f"<description>{channel_title}</description>"
        f"{items}"
        "</channel></rss>"
    )
    return Response(content=xml, media_type="application/rss+xml")


portal_router = APIRouter(prefix="/portal/blog", tags=["blog:portal"])


# ---- categories ----


@portal_router.post("/categories", status_code=status.HTTP_201_CREATED)
async def create_category(
    data: CategoryCreate,
    tenant: TenantDep,
    service: BlogServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> CategoryOut:
    return CategoryOut.model_validate(await service.create_category(tenant, data))


@portal_router.get("/categories")
async def list_categories(
    tenant: TenantDep,
    service: BlogServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> list[CategoryOut]:
    rows = await service.list_categories(tenant)
    return [CategoryOut.model_validate(r) for r in rows]


@portal_router.get("/categories/{category_id}")
async def get_category(
    category_id: uuid.UUID,
    tenant: TenantDep,
    service: BlogServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> CategoryOut:
    return CategoryOut.model_validate(await service.get_category(tenant, category_id))


@portal_router.patch("/categories/{category_id}")
async def update_category(
    category_id: uuid.UUID,
    data: CategoryUpdate,
    tenant: TenantDep,
    service: BlogServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> CategoryOut:
    return CategoryOut.model_validate(await service.update_category(tenant, category_id, data))


@portal_router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: uuid.UUID,
    tenant: TenantDep,
    service: BlogServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> None:
    await service.delete_category(tenant, category_id)


# ---- posts ----


@portal_router.post("/posts", status_code=status.HTTP_201_CREATED)
async def create_post(
    data: PostCreate,
    tenant: TenantDep,
    service: BlogServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> PostOut:
    return PostOut.model_validate(await service.create_post(tenant, data))


@portal_router.get("/posts")
async def list_posts(
    tenant: TenantDep,
    service: BlogServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
    status_filter: BlogPostStatus | None = Query(default=None, alias="status"),
    category_id: uuid.UUID | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[PostOut]:
    items, next_cursor, total = await service.list_posts(
        tenant, cursor=cursor, limit=limit, status=status_filter, category_id=category_id
    )
    return Page(
        items=[PostOut.model_validate(x) for x in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )


@portal_router.get("/posts/{post_id}")
async def get_post(
    post_id: uuid.UUID,
    tenant: TenantDep,
    service: BlogServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> PostOut:
    return PostOut.model_validate(await service.get_post(tenant, post_id))


@portal_router.patch("/posts/{post_id}")
async def update_post(
    post_id: uuid.UUID,
    data: PostUpdate,
    tenant: TenantDep,
    service: BlogServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> PostOut:
    return PostOut.model_validate(await service.update_post(tenant, post_id, data))


@portal_router.delete("/posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_post(
    post_id: uuid.UUID,
    tenant: TenantDep,
    service: BlogServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> None:
    await service.delete_post(tenant, post_id)


@portal_router.post("/posts/{post_id}/publish")
async def publish_post(
    post_id: uuid.UUID,
    tenant: TenantDep,
    service: BlogServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> PostOut:
    return PostOut.model_validate(await service.publish_post(tenant, post_id))


@portal_router.post("/posts/{post_id}/unpublish")
async def unpublish_post(
    post_id: uuid.UUID,
    tenant: TenantDep,
    service: BlogServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.CONTENT_MANAGE)),
) -> PostOut:
    return PostOut.model_validate(await service.unpublish_post(tenant, post_id))
