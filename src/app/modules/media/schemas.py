"""Pydantic schemas for listing media (§8.2).

Upload requests declare kind + content type + size up front so the API can
reject bad requests before any bytes move; the pipeline re-verifies the real
bytes (magic sniffing) after upload — the declaration is a claim, not proof.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from app.core.i18n import SUPPORTED_LOCALES, pick_localized
from app.core.schema import InputSchema, OutSchema, reject_null_for
from app.modules.media.models import (
    EMBED_KINDS,
    UPLOAD_KINDS,
    ListingMedia,
    MediaStatus,
)

# Explicit re-export: other modules read media enums from this schema module —
# the module boundary rule (§5) keeps models.py imports internal.
from app.modules.media.models import MediaKind as MediaKind

I18nText = dict[str, str]

# Accepted declared types per upload kind; the worker verifies magic bytes.
MEDIA_CONTENT_TYPES: dict[MediaKind, frozenset[str]] = {
    MediaKind.PHOTO: frozenset({"image/jpeg", "image/png", "image/webp"}),
    MediaKind.FLOORPLAN: frozenset({"image/jpeg", "image/png", "image/webp", "application/pdf"}),
    MediaKind.DOC: frozenset({"application/pdf"}),
}

# v1 embeds: store the URL, never transcode (§8.2). Hosts are allowlisted —
# an embed URL ends up in an <iframe> on the public site.
EMBED_HOSTS: dict[MediaKind, frozenset[str]] = {
    MediaKind.VIDEO: frozenset(
        {"www.youtube.com", "youtube.com", "youtu.be", "vimeo.com", "player.vimeo.com"}
    ),
    MediaKind.TOUR_3D: frozenset({"my.matterport.com"}),
}


def _validate_alt_text(value: I18nText | None) -> I18nText | None:
    if value is None:
        return None
    unknown = set(value) - set(SUPPORTED_LOCALES)
    if unknown:
        raise ValueError(f"unsupported locale keys: {sorted(unknown)}")
    cleaned = {k: v.strip() for k, v in value.items() if v and v.strip()}
    for locale, text in cleaned.items():
        if len(text) > 200:
            raise ValueError(f"'{locale}' text exceeds 200 characters")
    return cleaned


class MediaUploadRequest(InputSchema):
    kind: MediaKind
    content_type: str
    size_bytes: int = Field(gt=0)
    alt_text: I18nText | None = None

    @field_validator("alt_text")
    @classmethod
    def valid_alt(cls, value: I18nText | None) -> I18nText | None:
        return _validate_alt_text(value)

    @model_validator(mode="after")
    def uploadable(self) -> Self:
        if self.kind not in UPLOAD_KINDS:
            raise ValueError(f"'{self.kind.value}' media is added as an embed URL, not a file")
        if self.content_type not in MEDIA_CONTENT_TYPES[self.kind]:
            allowed = sorted(MEDIA_CONTENT_TYPES[self.kind])
            raise ValueError(f"contentType must be one of {allowed} for {self.kind.value}")
        return self


class MediaEmbedCreate(InputSchema):
    kind: MediaKind
    url: str = Field(max_length=500)
    alt_text: I18nText | None = None

    @field_validator("alt_text")
    @classmethod
    def valid_alt(cls, value: I18nText | None) -> I18nText | None:
        return _validate_alt_text(value)

    @model_validator(mode="after")
    def embeddable(self) -> Self:
        if self.kind not in EMBED_KINDS:
            raise ValueError(f"'{self.kind.value}' media is uploaded as a file, not embedded")
        parts = urlsplit(self.url)
        if parts.scheme != "https":
            raise ValueError("embed URLs must be https")
        if parts.hostname not in EMBED_HOSTS[self.kind]:
            raise ValueError(f"embed host not allowed for {self.kind.value}")
        return self


class MediaUpdate(InputSchema):
    """PATCH payload — ``exclude_unset`` semantics like listings."""

    position: int | None = Field(default=None, ge=0, le=500)
    alt_text: I18nText | None = None
    is_cover: bool | None = None

    @field_validator("alt_text")
    @classmethod
    def valid_alt(cls, value: I18nText | None) -> I18nText | None:
        return _validate_alt_text(value)

    _reject_required_nulls = reject_null_for("position", "is_cover", "alt_text")


class MediaVariantOut(OutSchema):
    url: str
    width: int
    height: int


class MediaOut(OutSchema):
    """Portal shape: full i18n alt text, processing state included."""

    id: uuid.UUID
    listing_id: uuid.UUID
    kind: MediaKind
    status: MediaStatus
    content_type: str | None
    size_bytes: int | None
    variants: dict[str, MediaVariantOut]
    blurhash: str | None
    position: int
    alt_text: I18nText
    is_cover: bool
    embed_url: str | None
    error: str | None
    created_at: datetime

    @classmethod
    def from_media(cls, media: ListingMedia, url_for: Callable[[str], str]) -> "MediaOut":
        return cls(
            id=media.id,
            listing_id=media.listing_id,
            kind=media.kind,
            status=media.status,
            content_type=media.content_type,
            size_bytes=media.size_bytes,
            variants=_variants_out(media, url_for),
            blurhash=media.blurhash,
            position=media.position,
            alt_text=media.alt_text,
            is_cover=media.is_cover,
            embed_url=media.embed_url,
            error=media.error,
            created_at=media.created_at,
        )


class MediaUploadOut(OutSchema):
    media: MediaOut
    upload_url: str
    # The client must send exactly these headers on the PUT or the presigned
    # signature will not match.
    upload_headers: dict[str, str]
    expires_in_seconds: int


class MediaDownloadOut(OutSchema):
    download_url: str
    expires_in_seconds: int


class PublicMediaOut(OutSchema):
    """Public shape: one negotiated alt-text locale, ready media only."""

    id: uuid.UUID
    kind: MediaKind
    variants: dict[str, MediaVariantOut]
    blurhash: str | None
    position: int
    alt: str | None
    is_cover: bool
    embed_url: str | None

    @classmethod
    def from_media(
        cls, media: ListingMedia, locale: str, url_for: Callable[[str], str]
    ) -> "PublicMediaOut":
        return cls(
            id=media.id,
            kind=media.kind,
            variants=_variants_out(media, url_for),
            blurhash=media.blurhash,
            position=media.position,
            alt=pick_localized(media.alt_text, locale),
            is_cover=media.is_cover,
            embed_url=media.embed_url,
        )


def _variants_out(media: ListingMedia, url_for: Callable[[str], str]) -> dict[str, MediaVariantOut]:
    return {
        name: MediaVariantOut(url=url_for(v["key"]), width=v["width"], height=v["height"])
        for name, v in media.variants.items()
    }
