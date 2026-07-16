"""Media processing pipeline (§8.2) — runs on the dedicated ``media`` queue.

``process_media`` pulls the confirmed original from the private bucket and:
1. verifies **magic bytes** against the declared content type (an extension
   or Content-Type header is a claim, not proof) and the real size cap;
2. derives variants with libvips — thumb 320w / card 640w / gallery 1280w /
   full 1920w, each as WebP + JPEG — auto-rotated and with **all metadata
   stripped** (EXIF GPS privacy) since only the private original keeps it;
3. computes a **blurhash** placeholder for the frontend;
4. uploads variants under content-hashed keys (immutable CDN caching) and
   flips the row to ``ready``.

Validation failures mark the row ``failed`` and delete the original — they
never retry. Only infrastructure errors (storage/DB down) retry.
"""

import hashlib
import uuid
from typing import Any

import blurhash
import pyvips
import structlog
from botocore.exceptions import ClientError
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.storage import ObjectStorage, create_storage
from app.modules.media.models import ListingMedia, MediaStatus
from app.workers.db import run_scoped

logger = structlog.get_logger(__name__)

VARIANT_WIDTHS = {"thumb": 320, "card": 640, "gallery": 1280, "full": 1920}
_FORMATS = (("webp", ".webp", "image/webp"), ("jpeg", ".jpg", "image/jpeg"))

# content type → (magic offset, magic bytes) — everything MEDIA_CONTENT_TYPES allows.
_MAGIC: dict[str, list[tuple[int, bytes]]] = {
    "image/jpeg": [(0, b"\xff\xd8\xff")],
    "image/png": [(0, b"\x89PNG\r\n\x1a\n")],
    "image/webp": [(0, b"RIFF"), (8, b"WEBP")],
    "application/pdf": [(0, b"%PDF")],
}


class MediaValidationError(Exception):
    """The uploaded object is not what was declared — permanent, never retried."""


def _verify_magic(data: bytes, content_type: str) -> None:
    signatures = _MAGIC.get(content_type)
    if signatures is None:
        raise MediaValidationError("unsupported content type")
    for offset, magic in signatures:
        if data[offset : offset + len(magic)] != magic:
            raise MediaValidationError("file bytes do not match the declared content type")


def _blurhash_of(image: pyvips.Image) -> str:
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


def _derive_variants(
    original: bytes, key_prefix: str, storage: ObjectStorage
) -> tuple[dict[str, Any], str]:
    """Generate + upload all variants; returns (variants JSONB, blurhash)."""
    try:
        # thumbnail auto-rotates from EXIF orientation before we strip it.
        probe = pyvips.Image.thumbnail_buffer(original, VARIANT_WIDTHS["full"], size="down")
    except pyvips.Error as exc:
        raise MediaValidationError("file could not be decoded as an image") from exc

    blur = _blurhash_of(probe)
    variants: dict[str, Any] = {}
    for name, width in VARIANT_WIDTHS.items():
        # copy_memory(): thumbnail output is a sequential pipeline that can be
        # read once — saving it twice (WebP + JPEG) needs a materialized image.
        image = pyvips.Image.thumbnail_buffer(original, width, size="down").copy_memory()
        for fmt, extension, mime in _FORMATS:
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


async def _load_media(session: AsyncSession, media_id: uuid.UUID) -> ListingMedia | None:
    stmt = select(ListingMedia).where(ListingMedia.id == media_id)
    return (await session.execute(stmt)).scalar_one_or_none()


@shared_task(
    name="app.workers.tasks.media.process_media",
    max_retries=3,
    # Only infrastructure errors escape the body (validation failures are
    # handled inside and marked `failed`), so a blanket autoretry is safe.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
)
def process_media(media_id: str, tenant_id: str) -> str:
    settings = get_settings()
    storage = create_storage(settings)
    tid = uuid.UUID(tenant_id)
    mid = uuid.UUID(media_id)

    async def _snapshot(session: AsyncSession) -> tuple[str, str, str] | None:
        media = await _load_media(session, mid)
        if media is None or media.status is not MediaStatus.PROCESSING:
            return None
        assert media.storage_key is not None and media.content_type is not None
        return media.storage_key, media.content_type, str(media.listing_id)

    snapshot = run_scoped(tid, _snapshot)
    if snapshot is None:
        # Idempotency: deleted, already processed, or double-delivered.
        return "skipped"
    storage_key, content_type, listing_id = snapshot

    try:
        try:
            # Size check via HEAD *before* buffering: the declared sizeBytes
            # was only a claim, and a presigned PUT cannot cap Content-Length —
            # never pull an unbounded object into worker memory.
            if storage.object_size(storage.docs_bucket, storage_key) > (
                settings.media_max_upload_bytes
            ):
                raise MediaValidationError("file exceeds the maximum upload size")
            original = storage.get_object(storage.docs_bucket, storage_key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise MediaValidationError("no uploaded file found for this media") from exc
            raise  # infrastructure problem — let Celery retry

        _verify_magic(original, content_type)

        variants: dict[str, Any] = {}
        blur: str | None = None
        if content_type != "application/pdf":
            key_prefix = f"tenants/{tenant_id}/listings/{listing_id}/{media_id}"
            variants, blur = _derive_variants(original, key_prefix, storage)
    except MediaValidationError as exc:
        reason = str(exc)
        storage.delete_objects(storage.docs_bucket, [storage_key])

        async def _mark_failed(session: AsyncSession) -> None:
            media = await _load_media(session, mid)
            if media is not None and media.status is MediaStatus.PROCESSING:
                media.status = MediaStatus.FAILED
                media.error = reason
                media.storage_key = None

        run_scoped(tid, _mark_failed)
        logger.info("media_rejected", media_id=media_id, reason=reason)
        return "failed"

    size = len(original)

    async def _mark_ready(session: AsyncSession) -> bool:
        media = await _load_media(session, mid)
        if media is None or media.status is not MediaStatus.PROCESSING:
            return False
        media.status = MediaStatus.READY
        media.variants = variants
        media.blurhash = blur
        media.size_bytes = size
        return True

    if not run_scoped(tid, _mark_ready):
        # The row was deleted mid-flight — its delete task only knew about the
        # original, so the variants uploaded above are ours to clean up.
        storage.delete_objects(storage.media_bucket, [v["key"] for v in variants.values()])
        return "skipped"
    logger.info("media_processed", media_id=media_id, variants=len(variants))
    return "ready"


@shared_task(
    name="app.workers.tasks.media.delete_media_objects",
    max_retries=5,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
)
def delete_media_objects(objects: list[list[str]]) -> int:
    """Remove a deleted media row's objects. Args are primitives ([bucket,
    key] pairs) so the task survives a broker restart (§12)."""
    storage = create_storage(get_settings())
    by_bucket: dict[str, list[str]] = {}
    for bucket, key in objects:
        by_bucket.setdefault(bucket, []).append(key)
    for bucket, keys in by_bucket.items():
        storage.delete_objects(bucket, keys)
    return len(objects)
