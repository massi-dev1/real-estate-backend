"""Listing media (§6.3 `listing_media`, §8.2 pipeline). Tenant-owned and
RLS-protected like every listing table.

Two families of rows:
- uploads (photo / floorplan / doc): born ``pending`` with a ``storage_key``
  pointing at the private originals bucket, move ``processing → ready`` when
  the Celery pipeline has verified + derived variants;
- embeds (video / tour_3d): a validated external URL, ``ready`` at birth —
  v1 stores YouTube/Vimeo/Matterport links, never transcodes (§8.2).
"""

import enum
import uuid
from typing import Any

from sqlalchemy import BigInteger, Enum, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MediaKind(enum.StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    TOUR_3D = "tour_3d"
    FLOORPLAN = "floorplan"
    DOC = "doc"


# Kinds that arrive as uploaded files vs. external embed URLs.
UPLOAD_KINDS = frozenset({MediaKind.PHOTO, MediaKind.FLOORPLAN, MediaKind.DOC})
EMBED_KINDS = frozenset({MediaKind.VIDEO, MediaKind.TOUR_3D})
# Kinds whose processed variants are public; floorplans/docs stay private (§8.2).
PUBLIC_KINDS = frozenset({MediaKind.PHOTO, MediaKind.VIDEO, MediaKind.TOUR_3D})


class MediaStatus(enum.StrEnum):
    PENDING = "pending"  # presigned URL issued, upload not confirmed
    PROCESSING = "processing"  # confirmed, pipeline queued/running
    READY = "ready"
    FAILED = "failed"  # validation failed; `error` says why


def _str_enum(enum_cls: type[enum.StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=20,
        values_callable=lambda e: [m.value for m in e],
    )


class ListingMedia(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "listing_media"
    __table_args__ = (
        Index("ix_listing_media_tenant_listing", "tenant_id", "listing_id", "position"),
        # At most one cover per listing — races on "set cover" hit the index,
        # not last-writer-wins.
        Index(
            "uq_listing_media_cover",
            "tenant_id",
            "listing_id",
            unique=True,
            postgresql_where=text("is_cover"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    listing_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    kind: Mapped[MediaKind] = mapped_column(_str_enum(MediaKind, "media_kind"))
    status: Mapped[MediaStatus] = mapped_column(
        _str_enum(MediaStatus, "media_status"),
        default=MediaStatus.PENDING,
        server_default=MediaStatus.PENDING.value,
    )

    # Original object in the private bucket (uploads); NULL for embeds.
    storage_key: Mapped[str | None] = mapped_column(String(300))
    embed_url: Mapped[str | None] = mapped_column(String(500))
    content_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)

    # {"gallery_webp": {"key": ..., "width": ..., "height": ...}, ...} (§8.2).
    variants: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    blurhash: Mapped[str | None] = mapped_column(String(60))

    position: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # i18n alt text, same {locale: text} shape as listing titles.
    alt_text: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    is_cover: Mapped[bool] = mapped_column(default=False, server_default=text("false"))

    error: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
