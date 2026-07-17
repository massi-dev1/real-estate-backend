"""Shared image-pipeline helpers for the ``media`` queue tasks (§8.2).

Extracted from the listing-media task when agent profile photos (§8.5) needed
the same claim-then-verify treatment: magic-byte sniffing, libvips variant
derivation with all metadata stripped (EXIF GPS privacy), content-hashed
public keys, and blurhash placeholders.
"""

import hashlib
from typing import Any

import blurhash
import pyvips

from app.core.storage import ObjectStorage

# format → (extension, mime) pairs used by variant derivation.
FORMATS_WEBP_JPEG = (("webp", ".webp", "image/webp"), ("jpeg", ".jpg", "image/jpeg"))

# content type → (magic offset, magic bytes) — everything the API allows.
_MAGIC: dict[str, list[tuple[int, bytes]]] = {
    "image/jpeg": [(0, b"\xff\xd8\xff")],
    "image/png": [(0, b"\x89PNG\r\n\x1a\n")],
    "image/webp": [(0, b"RIFF"), (8, b"WEBP")],
    "application/pdf": [(0, b"%PDF")],
}


class MediaValidationError(Exception):
    """The uploaded object is not what was declared — permanent, never retried."""


def verify_magic(data: bytes, content_type: str) -> None:
    signatures = _MAGIC.get(content_type)
    if signatures is None:
        raise MediaValidationError("unsupported content type")
    for offset, magic in signatures:
        if data[offset : offset + len(magic)] != magic:
            raise MediaValidationError("file bytes do not match the declared content type")


def blurhash_of(image: pyvips.Image) -> str:
    """Blurhash from a tiny render — pixel lists are fine at 32px wide."""
    small = image.thumbnail_image(32)
    if small.bands > 3:
        small = small.flatten(background=[255, 255, 255])
    if small.bands < 3:
        small = small.colourspace("srgb")
    mem = bytes(small.write_to_memory())
    width, height, bands = small.width, small.height, small.bands
    pixels = [
        [
            [mem[(row * width + col) * bands + band] for band in range(3)]
            for col in range(width)
        ]
        for row in range(height)
    ]
    return str(blurhash.encode(pixels, components_x=4, components_y=3, linear=False))


def derive_variants(
    original: bytes,
    key_prefix: str,
    storage: ObjectStorage,
    *,
    widths: dict[str, int],
    formats: tuple[tuple[str, str, str], ...] = FORMATS_WEBP_JPEG,
    with_blurhash: bool = True,
) -> tuple[dict[str, Any], str | None]:
    """Generate + upload all variants; returns (variants JSONB, blurhash)."""
    try:
        # thumbnail auto-rotates from EXIF orientation before we strip it.
        probe = pyvips.Image.thumbnail_buffer(original, max(widths.values()), size="down")
    except pyvips.Error as exc:
        raise MediaValidationError("file could not be decoded as an image") from exc

    blur = blurhash_of(probe) if with_blurhash else None
    variants: dict[str, Any] = {}
    for name, width in widths.items():
        # copy_memory(): thumbnail output is a sequential pipeline that can be
        # read once — saving it more than once needs a materialized image.
        image = pyvips.Image.thumbnail_buffer(original, width, size="down").copy_memory()
        for fmt, extension, mime in formats:
            # keep="none" drops every metadata block — EXIF GPS included (§8.2).
            buf = bytes(image.write_to_buffer(extension, Q=82, keep="none"))
            digest = hashlib.sha256(buf).hexdigest()[:12]
            key = f"{key_prefix}/{digest}-{name}{extension}"
            storage.put_object(storage.media_bucket, key, buf, mime)
            variants[f"{name}_{fmt}"] = {
                "key": key,
                "width": image.width,
                "height": image.height,
            }
    return variants, blur
