"""Agent profile photo processing (§8.5) — runs on the ``media`` queue.

The slim cousin of ``process_media``: same claim-then-verify pipeline (HEAD
size check before buffering, magic-byte sniff, metadata strip incl. EXIF GPS),
but only two public variants (avatar 320 / card 640) and no blurhash.
Validation failures mark the profile's photo ``failed`` and delete the
original — permanent, never retried; infrastructure errors retry.
"""

import uuid

import structlog
from botocore.exceptions import ClientError
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.storage import create_storage
from app.modules.agents.models import AgentProfile, PhotoStatus
from app.workers.db import run_scoped
from app.workers.tasks.images import MediaValidationError, derive_variants, verify_magic

logger = structlog.get_logger(__name__)

PHOTO_WIDTHS = {"avatar": 320, "card": 640}


async def _load_profile(session: AsyncSession, profile_id: uuid.UUID) -> AgentProfile | None:
    stmt = select(AgentProfile).where(AgentProfile.id == profile_id)
    return (await session.execute(stmt)).scalar_one_or_none()


@shared_task(
    name="app.workers.tasks.agents.process_agent_photo",
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=120,
)
def process_agent_photo(profile_id: str, tenant_id: str) -> str:
    settings = get_settings()
    storage = create_storage(settings)
    tid = uuid.UUID(tenant_id)
    pid = uuid.UUID(profile_id)

    async def _snapshot(session: AsyncSession) -> tuple[str, str] | None:
        profile = await _load_profile(session, pid)
        if profile is None or profile.photo_status is not PhotoStatus.PROCESSING:
            return None
        assert profile.photo_key is not None
        return profile.photo_key, str(profile.id)

    snapshot = run_scoped(tid, _snapshot)
    if snapshot is None:
        # Idempotency: deleted, already processed, or double-delivered.
        return "skipped"
    photo_key, _ = snapshot

    try:
        try:
            # Size check via HEAD *before* buffering (a presigned PUT cannot
            # cap Content-Length) — same stance as process_media.
            if storage.object_size(storage.docs_bucket, photo_key) > (
                settings.media_max_upload_bytes
            ):
                raise MediaValidationError("file exceeds the maximum upload size")
            original = storage.get_object(storage.docs_bucket, photo_key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise MediaValidationError("no uploaded file found for this photo") from exc
            raise  # infrastructure problem — let Celery retry

        # The content type was pinned by the presigned PUT; sniff against every
        # image type the API accepts rather than trusting the claim.
        for content_type in ("image/jpeg", "image/png", "image/webp"):
            try:
                verify_magic(original, content_type)
                break
            except MediaValidationError:
                continue
        else:
            raise MediaValidationError("file bytes are not a supported image")

        key_prefix = f"tenants/{tenant_id}/agents/{profile_id}"
        variants, _blur = derive_variants(
            original, key_prefix, storage, widths=PHOTO_WIDTHS, with_blurhash=False
        )
    except MediaValidationError as exc:
        reason = str(exc)
        storage.delete_objects(storage.docs_bucket, [photo_key])

        async def _mark_failed(session: AsyncSession) -> None:
            profile = await _load_profile(session, pid)
            if profile is not None and profile.photo_status is PhotoStatus.PROCESSING:
                profile.photo_status = PhotoStatus.FAILED
                profile.photo_error = reason
                profile.photo_key = None

        run_scoped(tid, _mark_failed)
        logger.info("agent_photo_rejected", profile_id=profile_id, reason=reason)
        return "failed"

    async def _mark_ready(session: AsyncSession) -> bool:
        profile = await _load_profile(session, pid)
        if profile is None or profile.photo_status is not PhotoStatus.PROCESSING:
            return False
        profile.photo_status = PhotoStatus.READY
        profile.photo_variants = variants
        profile.photo_error = None
        return True

    if not run_scoped(tid, _mark_ready):
        # Deleted (or replaced) mid-flight — the just-uploaded variants are
        # ours to clean up (same race close as process_media, Part 6 finding).
        keys: list[str] = [v["key"] for v in variants.values()]
        storage.delete_objects(storage.media_bucket, keys)
        return "skipped"
    logger.info("agent_photo_processed", profile_id=profile_id, variants=len(variants))
    return "ready"
