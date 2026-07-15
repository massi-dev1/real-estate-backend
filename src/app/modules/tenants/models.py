"""Tenants and their domains — global (platform-level) tables, no RLS (§4.3).

The tenant-resolution middleware must query these *before* any tenant context
exists, so they are deliberately outside row-level security.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Enum, ForeignKey, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TenantStatus(enum.StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Tenant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    status: Mapped[TenantStatus] = mapped_column(
        Enum(
            TenantStatus,
            name="tenant_status",
            native_enum=False,
            length=20,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=TenantStatus.TRIAL,
        server_default=TenantStatus.TRIAL.value,
    )
    # Branding, locales, currency, feature toggles — validated shape comes with
    # the site-config work (§4.4); stored as JSONB from day one.
    settings: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )

    domains: Mapped[list["TenantDomain"]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
        order_by="TenantDomain.created_at",
    )


class TenantDomain(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tenant_domains"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    domain: Mapped[str] = mapped_column(String(253), unique=True)
    is_primary: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    verified_at: Mapped[datetime | None]

    tenant: Mapped[Tenant] = relationship(back_populates="domains")
