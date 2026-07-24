"""Blog (§8.10, slice 2) — categories + posts, tenant-owned and RLS-protected
(migration 0014).

``blog_categories`` is a small curated taxonomy (i18n name, per-tenant slug) a
post references at most one of (``category_id`` FK, ``ON DELETE SET NULL`` — a
deleted category must not cascade-delete its posts).

``blog_posts`` carries i18n ``title``/``excerpt``/``body`` (body is sanitized
HTML per locale — the first rich-text surface in the codebase, §10 XSS rule),
free-form ``tags`` (GIN-indexed for containment filtering like listings'
``features``), and a three-state ``status`` (draft/scheduled/published). A
``SCHEDULED`` post is flipped to ``PUBLISHED`` by the Beat sweep once
``scheduled_at`` is due; ``published_at`` is stamped once and never reset.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Enum, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BlogPostStatus(enum.StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"


def _str_enum(enum_cls: type[enum.StrEnum], name: str, length: int = 30) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class BlogCategory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "blog_categories"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_blog_categories_tenant_slug"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(160))
    # i18n name: {"ar": ..., "fr": ..., "en": ...} (§8.1).
    name: Mapped[dict[str, Any]] = mapped_column()


class BlogPost(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "blog_posts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_blog_posts_tenant_slug"),
        # Covers the public list / RSS query (published, newest first).
        Index("ix_blog_posts_tenant_status_published", "tenant_id", "status", "published_at"),
        # Containment filtering on tags — same pattern as listings' features.
        Index("ix_blog_posts_tags", "tags", postgresql_using="gin"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("blog_categories.id", ondelete="SET NULL"), index=True
    )
    slug: Mapped[str] = mapped_column(String(160))
    # i18n content fields (§8.1). body holds sanitized HTML per locale.
    title: Mapped[dict[str, Any]] = mapped_column()
    excerpt: Mapped[dict[str, Any] | None] = mapped_column()
    body: Mapped[dict[str, Any]] = mapped_column()
    # Explicit JSONB: the type registry maps dict[str, Any] but not list[...].
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    cover_image: Mapped[str | None] = mapped_column(String(500))
    # {title: {i18n}, description: {i18n}, og_image: url} — same shape as pages.
    seo_meta: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[BlogPostStatus] = mapped_column(
        _str_enum(BlogPostStatus, "blog_post_status"),
        default=BlogPostStatus.DRAFT,
        server_default=BlogPostStatus.DRAFT.value,
    )
    # Future go-live for a SCHEDULED post; the Beat sweep publishes it when due.
    scheduled_at: Mapped[datetime | None]
    # Stamped on first publish (manual or sweep), never reset — powers sitemap
    # lastmod, RSS pubDate, and the "published since" ordering.
    published_at: Mapped[datetime | None]

    # Read-only convenience for public projection (slug + i18n name). Eager
    # loaded on the public detail/list queries so the public schema can resolve
    # the category without a second round trip. No back_populates — the
    # category never needs to enumerate its posts in-Python.
    category: Mapped["BlogCategory | None"] = relationship(lazy="raise", viewonly=True)
