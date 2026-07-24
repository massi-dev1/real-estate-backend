"""DB access for blog categories and posts. Every method takes ``tenant_id``
(golden rule §5); RLS is the safety net, not the only guard.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.blog.models import BlogCategory, BlogPost, BlogPostStatus


class BlogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, obj: BlogCategory | BlogPost) -> None:
        self.session.add(obj)

    async def flush(self) -> None:
        await self.session.flush()

    # ---- categories ----

    async def get_category(
        self, tenant_id: uuid.UUID, category_id: uuid.UUID
    ) -> BlogCategory | None:
        stmt = select(BlogCategory).where(
            BlogCategory.tenant_id == tenant_id, BlogCategory.id == category_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_category_by_slug(self, tenant_id: uuid.UUID, slug: str) -> BlogCategory | None:
        stmt = select(BlogCategory).where(
            BlogCategory.tenant_id == tenant_id, BlogCategory.slug == slug
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_categories(self, tenant_id: uuid.UUID) -> list[BlogCategory]:
        # A curated nav list is small — no pagination needed.
        stmt = (
            select(BlogCategory)
            .where(BlogCategory.tenant_id == tenant_id)
            .order_by(BlogCategory.created_at)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def delete_category(self, category: BlogCategory) -> None:
        await self.session.delete(category)

    # ---- posts ----

    async def get_post(self, tenant_id: uuid.UUID, post_id: uuid.UUID) -> BlogPost | None:
        stmt = select(BlogPost).where(BlogPost.tenant_id == tenant_id, BlogPost.id == post_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_published_post_by_slug(self, tenant_id: uuid.UUID, slug: str) -> BlogPost | None:
        stmt = (
            select(BlogPost)
            .where(
                BlogPost.tenant_id == tenant_id,
                BlogPost.slug == slug,
                BlogPost.status == BlogPostStatus.PUBLISHED,
            )
            .options(selectinload(BlogPost.category))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_posts(
        self,
        tenant_id: uuid.UUID,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
        status: BlogPostStatus | None = None,
        category_id: uuid.UUID | None = None,
    ) -> list[BlogPost]:
        """Portal keyset page on (created_at DESC, id DESC); returns limit+1 rows."""
        stmt = select(BlogPost).where(BlogPost.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(BlogPost.status == status)
        if category_id is not None:
            stmt = stmt.where(BlogPost.category_id == category_id)
        if after is not None:
            stmt = stmt.where(
                (BlogPost.created_at < after[0])
                | ((BlogPost.created_at == after[0]) & (BlogPost.id < after[1]))
            )
        stmt = stmt.order_by(BlogPost.created_at.desc(), BlogPost.id.desc()).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    async def count_posts(self, tenant_id: uuid.UUID) -> int:
        stmt = select(func.count()).select_from(BlogPost).where(BlogPost.tenant_id == tenant_id)
        return (await self.session.execute(stmt)).scalar_one()

    async def list_public_posts(
        self,
        tenant_id: uuid.UUID,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
        category_slug: str | None = None,
        tag: str | None = None,
    ) -> list[BlogPost]:
        """Public keyset page on (published_at DESC, id DESC), published only;
        returns limit+1 rows. Category joined by slug, tag by JSONB containment."""
        stmt = (
            select(BlogPost)
            .where(
                BlogPost.tenant_id == tenant_id,
                BlogPost.status == BlogPostStatus.PUBLISHED,
            )
            .options(selectinload(BlogPost.category))
        )
        if category_slug is not None:
            stmt = stmt.join(BlogCategory, BlogPost.category_id == BlogCategory.id).where(
                BlogCategory.slug == category_slug
            )
        if tag is not None:
            stmt = stmt.where(BlogPost.tags.contains([tag]))
        if after is not None:
            stmt = stmt.where(
                (BlogPost.published_at < after[0])
                | ((BlogPost.published_at == after[0]) & (BlogPost.id < after[1]))
            )
        stmt = stmt.order_by(BlogPost.published_at.desc(), BlogPost.id.desc()).limit(limit + 1)
        return list((await self.session.execute(stmt)).scalars())

    async def published_posts_for_sitemap(
        self, tenant_id: uuid.UUID, *, limit: int
    ) -> list[BlogPost]:
        stmt = (
            select(BlogPost)
            .where(
                BlogPost.tenant_id == tenant_id,
                BlogPost.status == BlogPostStatus.PUBLISHED,
            )
            .order_by(BlogPost.published_at.desc(), BlogPost.id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def recent_published_for_rss(self, tenant_id: uuid.UUID, *, limit: int) -> list[BlogPost]:
        stmt = (
            select(BlogPost)
            .where(
                BlogPost.tenant_id == tenant_id,
                BlogPost.status == BlogPostStatus.PUBLISHED,
            )
            .order_by(BlogPost.published_at.desc(), BlogPost.id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def due_scheduled_posts(self, tenant_id: uuid.UUID, *, now: datetime) -> list[BlogPost]:
        """SCHEDULED posts past their go-live — worker-only. The status filter
        is the sweep's idempotency guard (a re-run no longer matches published
        rows)."""
        stmt = select(BlogPost).where(
            BlogPost.tenant_id == tenant_id,
            BlogPost.status == BlogPostStatus.SCHEDULED,
            BlogPost.scheduled_at.is_not(None),
            BlogPost.scheduled_at <= now,
        )
        return list((await self.session.execute(stmt)).scalars())

    async def delete_post(self, post: BlogPost) -> None:
        await self.session.delete(post)
