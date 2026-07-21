"""Content CMS (§8.10, slice 1) — structured pages + versioned legal pages.
Two tenant-owned, RLS-protected tables (migration 0013).

``content_pages`` backs the agency site: home blocks, about, buyers/sellers
pages. The frontend defines block types; the backend stores validated block
JSON (an ordered list of ``{type, data}``) — this is not a page-builder.

``legal_pages`` is **append-only and versioned**: every edit inserts a new
row, never mutates an old one, so an agency can always prove what a user
consented to and when (§10.12). A partial-unique index keeps exactly one
``is_current`` row per ``kind``.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Enum, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PageStatus(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class LegalKind(enum.StrEnum):
    PRIVACY = "privacy"
    TERMS = "terms"
    FAIR_TREATMENT = "fair_treatment"
    LICENSE_DISCLOSURE = "license_disclosure"


def _str_enum(enum_cls: type[enum.StrEnum], name: str, length: int = 30) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class ContentPage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "content_pages"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_content_pages_tenant_slug"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(160))
    # i18n content: {"ar": ..., "fr": ..., "en": ...} (§8.1).
    title: Mapped[dict[str, Any]] = mapped_column()
    # Ordered list of {type, data} blocks — envelope validated, stored as-is.
    # Explicit JSONB: the registry maps dict[str, Any] but not list[...].
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # {title: {i18n}, description: {i18n}, og_image: url}.
    seo_meta: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[PageStatus] = mapped_column(
        _str_enum(PageStatus, "page_status"),
        default=PageStatus.DRAFT,
        server_default=PageStatus.DRAFT.value,
    )
    # Set on first publish; powers the sitemap lastmod and "published since".
    published_at: Mapped[datetime | None]


class LegalPage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "legal_pages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "version", name="uq_legal_pages_tenant_kind_version"),
        # At most one current version per kind (partial unique).
        Index(
            "uq_legal_pages_current",
            "tenant_id",
            "kind",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[LegalKind] = mapped_column(_str_enum(LegalKind, "legal_kind"))
    version: Mapped[int] = mapped_column(Integer)
    # i18n body: {"ar": ..., "fr": ..., "en": ...}.
    body: Mapped[dict[str, Any]] = mapped_column()
    effective_at: Mapped[datetime]
    is_current: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
