"""Agents & teams (§6.2, §8.5). All tables are tenant-owned and RLS-protected
(migration 0008).

- ``agent_profiles`` — the public-directory face of an AGENT-role user: i18n
  bio, specialties, an optional PostGIS ``service_areas`` MultiPolygon (the
  territory-assignment source, GiST-indexed) and a processed profile photo.
- ``teams`` / ``team_members`` — §8.5 membership; powers ``team_lead``'s
  team-scoped visibility over listings and leads.
"""

import enum
import uuid
from typing import Any

from geoalchemy2 import Geometry, WKBElement, WKTElement
from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PhotoStatus(enum.StrEnum):
    """Same lifecycle as listing media (§8.2), minus embeds. NULL = no photo."""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


def _str_enum(enum_cls: type[enum.StrEnum], name: str, length: int = 20) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class AgentProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id"),
        UniqueConstraint("tenant_id", "slug"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    slug: Mapped[str] = mapped_column(String(120))
    # i18n content: {"ar": ..., "fr": ..., "en": ...} like listing titles.
    bio: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'::jsonb"))
    # Validated against the controlled vocabulary in schemas.
    specialties: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # Territory-assignment source (§8.4/§8.5): outer rings only, GiST-indexed.
    service_areas: Mapped[WKBElement | WKTElement | None] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False)
    )
    license_no: Mapped[str | None] = mapped_column(String(100))
    socials: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    is_published: Mapped[bool] = mapped_column(default=False, server_default=text("false"))

    # Profile photo — original in the private bucket, public variants derived
    # by the media queue (same claim-then-verify pipeline as listing photos).
    photo_key: Mapped[str | None] = mapped_column(String(300))
    photo_status: Mapped[PhotoStatus | None] = mapped_column(
        _str_enum(PhotoStatus, "agent_photo_status")
    )
    photo_variants: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    photo_error: Mapped[str | None] = mapped_column(String(200))


class Team(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "teams"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    lead_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class TeamMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "user_id"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role_in_team: Mapped[str | None] = mapped_column(String(40))
