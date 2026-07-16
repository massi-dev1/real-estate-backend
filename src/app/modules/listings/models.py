"""Listings — the core inventory (§6.3). All tables are tenant-owned and
RLS-protected; every query additionally filters ``tenant_id`` explicitly.

Deferred by design: ``search_vector`` + FTS (search part), ``neighborhood_id``
(content part), media tables (media part), geocoding (Celery part) — the API
accepts explicit coordinates until then.
"""

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geometry, WKBElement, WKTElement
from sqlalchemy import (
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ListingStatus(enum.StrEnum):
    DRAFT = "draft"
    REVIEW = "review"
    PUBLISHED = "published"
    RESERVED = "reserved"
    SOLD = "sold"
    RENTED = "rented"
    ARCHIVED = "archived"


class ListingPurpose(enum.StrEnum):
    SALE = "sale"
    RENT = "rent"
    RENT_DAILY = "rent_daily"


class PricePeriod(enum.StrEnum):
    MONTH = "month"
    DAY = "day"


class PropertyType(enum.StrEnum):
    APARTMENT = "apartment"
    HOUSE = "house"
    VILLA = "villa"
    STUDIO = "studio"
    DUPLEX = "duplex"
    LAND = "land"
    OFFICE = "office"
    RETAIL = "retail"
    WAREHOUSE = "warehouse"
    GARAGE = "garage"
    FARM = "farm"
    BUILDING = "building"
    OTHER = "other"


def _str_enum(enum_cls: type[enum.StrEnum], name: str, length: int = 20) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class Listing(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("tenant_id", "reference_code"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Human code agencies quote on the phone: "AGE-2026-00123" (§8.1).
    reference_code: Mapped[str] = mapped_column(String(24))
    agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[ListingStatus] = mapped_column(
        _str_enum(ListingStatus, "listing_status"),
        default=ListingStatus.DRAFT,
        server_default=ListingStatus.DRAFT.value,
    )
    purpose: Mapped[ListingPurpose] = mapped_column(_str_enum(ListingPurpose, "listing_purpose"))
    property_type: Mapped[PropertyType] = mapped_column(_str_enum(PropertyType, "property_type"))

    # i18n content: {"ar": ..., "fr": ..., "en": ...} (§8.1).
    title: Mapped[dict[str, Any]] = mapped_column()
    description: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )

    price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="DZD", server_default="DZD")
    price_period: Mapped[PricePeriod | None] = mapped_column(_str_enum(PricePeriod, "price_period"))
    negotiable: Mapped[bool] = mapped_column(default=False, server_default=text("false"))

    beds: Mapped[int | None] = mapped_column(SmallInteger)
    baths: Mapped[int | None] = mapped_column(SmallInteger)
    area_built: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    area_land: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    floor: Mapped[int | None] = mapped_column(SmallInteger)
    floors_total: Mapped[int | None] = mapped_column(SmallInteger)
    year_built: Mapped[int | None] = mapped_column(SmallInteger)

    # Validated against the controlled vocabulary in schemas (GIN-indexed).
    features: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    address: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    location: Mapped[WKBElement | WKTElement | None] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False)
    )

    published_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    view_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[datetime | None]


class ListingStatusHistory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only trail of workflow transitions (§8.1) — ``created_at`` is the
    transition time."""

    __tablename__ = "listing_status_history"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    listing_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[ListingStatus] = mapped_column(_str_enum(ListingStatus, "listing_status"))
    to_status: Mapped[ListingStatus] = mapped_column(_str_enum(ListingStatus, "listing_status"))
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class ListingReferenceCounter(Base):
    """Per-tenant, per-year sequence behind reference codes. Bumped with an
    atomic upsert — two concurrent creates can never mint the same number."""

    __tablename__ = "listing_reference_counters"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    year: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer)
