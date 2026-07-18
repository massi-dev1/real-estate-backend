"""Valuations (§8.8) — the seller lead magnet. One tenant-RLS table
(migration 0012).

``valuation_requests`` is filled progressively by the public multi-step form
(address → property details → contact): columns are nullable until their step
arrives, so a partial abandon still captures what was given. Completion
computes the comparable-sales estimate band and mints a CRM lead —
``contact_id``/``lead_id`` link into the CRM by column only; the module talks
to leads through its service, never its tables.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geometry, WKBElement, WKTElement
from sqlalchemy import Enum, ForeignKey, Numeric, SmallInteger, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Enum-only import: the schema vocabulary is shared platform-wide (same
# precedent as agents/schemas importing ListingStatus) — tables and
# repositories stay behind the listings service boundary.
from app.modules.listings.models import PropertyType


class ValuationRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "valuation_requests"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )

    # Step 1 — address. {street, city, postal_code} (city required at intake);
    # no geocoding exists, so the map-pin point is the only geo signal.
    address: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    location: Mapped[WKBElement | WKTElement | None] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False)
    )

    # Step 2 — property details (all optional until provided).
    property_type: Mapped[PropertyType | None] = mapped_column(
        Enum(
            PropertyType,
            name="property_type",
            native_enum=False,
            length=20,
            values_callable=lambda e: [m.value for m in e],
        )
    )
    area_built: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    beds: Mapped[int | None] = mapped_column(SmallInteger)
    baths: Mapped[int | None] = mapped_column(SmallInteger)
    floor: Mapped[int | None] = mapped_column(SmallInteger)
    year_built: Mapped[int | None] = mapped_column(SmallInteger)
    # Free-form extras (condition, features, notes) — stored, never queried.
    details: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )

    # Step 3 — completion: contact + computed estimate band. NULL band with a
    # non-NULL completed_at means "not enough comps — an agent follows up".
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    estimate_low: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    estimate_high: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), default="DZD", server_default="DZD")
    comps_count: Mapped[int | None] = mapped_column(SmallInteger)
    completed_at: Mapped[datetime | None]
