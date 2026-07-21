"""Scheduled-publish Beat job for the blog (§8.10, §12).

Runs every few minutes, once per tenant (RLS is fail-closed — there is no
cross-tenant query, even from a worker). A ``SCHEDULED`` post whose
``scheduled_at`` has passed is flipped to ``PUBLISHED`` and its ``published_at``
stamped. The status filter is the idempotency guard: once a post is published,
a retried or overlapping run no longer matches it.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial

import structlog
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.blog.models import BlogPost, BlogPostStatus
from app.modules.tenants.models import Tenant, TenantStatus
from app.workers.db import run_scoped, run_scoped_many

logger = structlog.get_logger(__name__)


async def _active_tenant_ids(session: AsyncSession) -> list[uuid.UUID]:
    stmt = select(Tenant.id).where(Tenant.status != TenantStatus.SUSPENDED)
    return list((await session.execute(stmt)).scalars())


async def _publish_tenant(session: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> int:
    stmt = select(BlogPost).where(
        BlogPost.tenant_id == tenant_id,
        BlogPost.status == BlogPostStatus.SCHEDULED,
        BlogPost.scheduled_at.is_not(None),
        BlogPost.scheduled_at <= now,
    )
    posts = list((await session.execute(stmt)).scalars())
    for post in posts:
        post.status = BlogPostStatus.PUBLISHED
        if post.published_at is None:
            post.published_at = now
    return len(posts)


@shared_task(name="app.workers.tasks.blog.publish_scheduled_posts")
def publish_scheduled_posts() -> int:
    """Idempotent: already-published posts no longer match the SCHEDULED
    filter, so a retry or an overlapping run cannot double-publish or
    re-stamp ``published_at``."""
    now = datetime.now(UTC)

    async def _list_tenants(session: AsyncSession) -> list[uuid.UUID]:
        return await _active_tenant_ids(session)

    tenant_ids = run_scoped(None, _list_tenants)

    # One shared engine for the whole batch — SET LOCAL isolates each tenant's
    # transaction, so a fresh engine per tenant would be pure overhead.
    calls: list[tuple[uuid.UUID | None, Callable[[AsyncSession], Awaitable[int]]]] = [
        (tid, partial(_publish_tenant, tenant_id=tid, now=now)) for tid in tenant_ids
    ]
    counts = run_scoped_many(calls)

    total = 0
    for tenant_id, count in zip(tenant_ids, counts, strict=True):
        total += count
        if count:
            logger.info("blog_posts_published", tenant_id=str(tenant_id), count=count)
    return total
