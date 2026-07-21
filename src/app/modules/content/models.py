"""Content CMS (§8.10) — structured pages, versioned legal pages,
neighborhood guides, and gated market reports.

``content_pages`` backs the agency site: home blocks, about, buyers/sellers
pages. The frontend defines block types; the backend stores validated block
JSON (an ordered list of ``{type, data}``) — this is not a page-builder.

``legal_pages`` is **append-only and versioned**: every edit inserts a new
row, never mutates an old one, so an agency can always prove what a user
consented to and when (§10.12). A partial-unique index keeps exactly one
``is_current`` row per ``kind``.

``neighborhood_guides`` (slice 3) carry i18n editorial content plus an
optional PostGIS ``boundary`` MultiPolygon. Listings "belong" to a guide by
``ST_Contains`` on their point — a live relationship, never a stored FK — and
a nightly Beat job recomputes ``stats`` (listing count + median price) inside
the polygon.

``market_reports`` (slice 3) carry author-supplied ``stats`` and a
worker-rendered PDF in the private bucket. The PDF URL is never public: a
public gate takes an email → mints a lead → returns a short-lived presigned
GET (§8.10 "email required to download → lead").
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry, WKBElement, WKTElement
from sqlalchemy import Enum, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PageStatus(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class ReportStatus(enum.StrEnum):
    """A published report's PDF is rendered off-thread; ``ready`` is set by the
    worker once the object lands in the private bucket."""

    DRAFT = "draft"
    PUBLISHED = "published"  # metadata live, PDF render enqueued
    READY = "ready"  # PDF rendered and uploaded — gate can serve it


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


class NeighborhoodGuide(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "neighborhood_guides"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_neighborhood_guides_tenant_slug"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(160))
    # i18n content: {"ar": ..., "fr": ..., "en": ...} (§8.1).
    name: Mapped[dict[str, Any]] = mapped_column()
    body: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    # Optional catchment polygon (outer rings only, GiST-indexed). Listings
    # inside it are auto-linked live via ST_Contains — never a stored FK.
    boundary: Mapped[WKBElement | WKTElement | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False)
    )
    # {title: {i18n}, description: {i18n}, og_image: url} — same shape as pages.
    seo_meta: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[PageStatus] = mapped_column(
        _str_enum(PageStatus, "page_status"),
        default=PageStatus.DRAFT,
        server_default=PageStatus.DRAFT.value,
    )
    # Computed by the nightly Beat job — never client-writable.
    # {"listing_count": int, "median_price": "..." | null}.
    stats: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    stats_computed_at: Mapped[datetime | None]
    published_at: Mapped[datetime | None]


class MarketReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "market_reports"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_market_reports_tenant_slug"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(160))
    # i18n title: {"ar": ..., "fr": ..., "en": ...}.
    title: Mapped[dict[str, Any]] = mapped_column()
    # Author-supplied charts/numbers the agency compiled — not auto-computed.
    stats: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    # Private-bucket key of the rendered PDF (like media documents); NULL until
    # the worker finishes rendering it.
    pdf_object_key: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[ReportStatus] = mapped_column(
        _str_enum(ReportStatus, "report_status"),
        default=ReportStatus.DRAFT,
        server_default=ReportStatus.DRAFT.value,
    )
    # Stamped when the PDF render completes (status → ready).
    generated_at: Mapped[datetime | None]
    published_at: Mapped[datetime | None]
