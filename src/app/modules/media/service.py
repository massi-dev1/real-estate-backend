"""Media business logic (§8.2).

Ownership scoping is delegated to the listings service: every operation first
resolves the target listing through ``ListingService.get_portal`` — an agent
who cannot see a listing gets the same 404 for its media (no existence
oracle). The file bytes themselves never pass through this process: uploads
go client → storage via presigned PUT, processing happens in the Celery
``media`` queue, and public delivery is CDN/anonymous-bucket reads.
"""

import uuid
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_utils.compat import uuid7

from app.core.config import Settings
from app.core.database import SessionDep, on_commit
from app.core.exceptions import ConflictError, NotFoundError, QuotaExceededError
from app.core.permissions import AuthenticatedUser
from app.core.storage import ObjectStorage
from app.core.tenancy import TenantContext
from app.modules.listings.service import ListingService, get_listing_service
from app.modules.media.models import (
    ListingMedia,
    MediaKind,
    MediaStatus,
)
from app.modules.media.repository import MediaRepository
from app.modules.media.schemas import MediaEmbedCreate, MediaUpdate, MediaUploadRequest
from app.workers.tasks.media import delete_media_objects, process_media

_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "application/pdf": "pdf",
}


class MediaService:
    def __init__(
        self,
        repo: MediaRepository,
        listings: ListingService,
        storage: ObjectStorage,
        settings: Settings,
    ) -> None:
        self.repo = repo
        self.listings = listings
        self.storage = storage
        self.settings = settings

    # ---- helpers ----

    async def _get_scoped_or_404(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        media_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> ListingMedia:
        media = await self.repo.get(tenant.id, media_id, for_update=for_update)
        if media is None:
            raise NotFoundError("Media not found.")
        # Listing-level scoping: not the actor's listing → same 404.
        try:
            await self.listings.get_portal(tenant, actor, media.listing_id)
        except NotFoundError:
            raise NotFoundError("Media not found.") from None
        return media

    def _photo_quota(self, tenant: TenantContext) -> int:
        media_settings: dict[str, Any] = tenant.settings.get("media") or {}
        quota = media_settings.get("max_photos_per_listing")
        return int(quota) if quota else self.settings.media_max_photos_per_listing

    # ---- upload flow ----

    async def request_upload(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        listing_id: uuid.UUID,
        data: MediaUploadRequest,
    ) -> tuple[ListingMedia, str, dict[str, str]]:
        # Row lock on the listing: the quota check below is count-then-insert,
        # and two concurrent presign requests must not both pass at quota-1
        # (same race pattern as listing transitions — Part 4 review finding).
        listing = await self.listings.get_portal(tenant, actor, listing_id, for_update=True)
        if data.size_bytes > self.settings.media_max_upload_bytes:
            limit_mb = self.settings.media_max_upload_bytes // (1024 * 1024)
            raise QuotaExceededError(f"Files larger than {limit_mb} MB are not accepted.")
        if data.kind is MediaKind.PHOTO:
            count = await self.repo.count_active_photos(tenant.id, listing.id)
            if count >= self._photo_quota(tenant):
                raise QuotaExceededError("This listing has reached its photo quota.")

        # The storage key embeds the id, so mint it before the INSERT.
        media_id = uuid7()
        media = ListingMedia(
            id=media_id,
            tenant_id=tenant.id,
            listing_id=listing.id,
            kind=data.kind,
            status=MediaStatus.PENDING,
            # Originals are private: EXIF (GPS!) is stripped only in variants.
            storage_key=f"tenants/{tenant.id}/listings/{listing.id}/{media_id}/original",
            content_type=data.content_type,
            size_bytes=data.size_bytes,
            alt_text=data.alt_text or {},
            position=await self.repo.next_position(tenant.id, listing.id),
            created_by=actor.id,
        )
        self.repo.add(media)
        await self.repo.flush()

        assert media.storage_key is not None
        upload_url = self.storage.presign_put(
            self.storage.docs_bucket, media.storage_key, data.content_type
        )
        return media, upload_url, {"Content-Type": data.content_type}

    async def confirm_upload(
        self, tenant: TenantContext, actor: AuthenticatedUser, media_id: uuid.UUID
    ) -> ListingMedia:
        media = await self._get_scoped_or_404(tenant, actor, media_id, for_update=True)
        if media.status is not MediaStatus.PENDING:
            raise ConflictError(f"This upload is already '{media.status.value}'.")
        media.status = MediaStatus.PROCESSING
        await self.repo.flush()

        # Enqueue only after the transaction commits — the task reads the row
        # from its own connection and must see the committed `processing` state.
        media_id_str, tenant_id_str = str(media.id), str(tenant.id)

        async def _enqueue() -> None:
            process_media.delay(media_id_str, tenant_id_str)

        on_commit(self.repo.session, _enqueue)
        return media

    # ---- embeds ----

    async def create_embed(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        listing_id: uuid.UUID,
        data: MediaEmbedCreate,
    ) -> ListingMedia:
        listing = await self.listings.get_portal(tenant, actor, listing_id)
        media = ListingMedia(
            tenant_id=tenant.id,
            listing_id=listing.id,
            kind=data.kind,
            status=MediaStatus.READY,  # nothing to process — URL only (§8.2)
            embed_url=data.url,
            alt_text=data.alt_text or {},
            position=await self.repo.next_position(tenant.id, listing.id),
            created_by=actor.id,
        )
        self.repo.add(media)
        await self.repo.flush()
        return media

    # ---- management ----

    async def list_for_listing(
        self, tenant: TenantContext, actor: AuthenticatedUser, listing_id: uuid.UUID
    ) -> list[ListingMedia]:
        listing = await self.listings.get_portal(tenant, actor, listing_id)
        return await self.repo.list_for_listing(tenant.id, listing.id)

    async def update(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        media_id: uuid.UUID,
        data: MediaUpdate,
    ) -> ListingMedia:
        media = await self._get_scoped_or_404(tenant, actor, media_id, for_update=True)
        patch = data.model_dump(exclude_unset=True)
        if patch.pop("is_cover", None) is not None:
            if data.is_cover:
                if media.kind is not MediaKind.PHOTO or media.status is not MediaStatus.READY:
                    raise ConflictError("Only a processed photo can be the cover.")
                await self.repo.clear_cover(tenant.id, media.listing_id)
                media.is_cover = True
            else:
                media.is_cover = False
        for field, value in patch.items():
            setattr(media, field, value)
        await self.repo.flush()
        return media

    async def delete(
        self, tenant: TenantContext, actor: AuthenticatedUser, media_id: uuid.UUID
    ) -> None:
        media = await self._get_scoped_or_404(tenant, actor, media_id, for_update=True)
        objects: list[list[str]] = []
        if media.storage_key:
            objects.append([self.storage.docs_bucket, media.storage_key])
        objects.extend(
            [self.storage.media_bucket, variant["key"]] for variant in media.variants.values()
        )
        await self.repo.delete(media)
        await self.repo.flush()

        if objects:
            # Storage cleanup happens after commit: if the transaction rolls
            # back, the row survives and its objects must too.
            async def _enqueue() -> None:
                delete_media_objects.delay(objects)

            on_commit(self.repo.session, _enqueue)

    async def download_url(
        self, tenant: TenantContext, actor: AuthenticatedUser, media_id: uuid.UUID
    ) -> str:
        """Presigned GET (15 min, §8.2) for the private original — the only
        read path for floor plans and documents."""
        media = await self._get_scoped_or_404(tenant, actor, media_id)
        if not media.storage_key:
            raise ConflictError("This media has no downloadable file.")
        extension = _EXTENSIONS.get(media.content_type or "", "bin")
        return self.storage.presign_get(
            self.storage.docs_bucket,
            media.storage_key,
            filename=f"{media.kind.value}-{media.id}.{extension}",
        )

    # ---- public site (no actor — published listings only, callers verify) ----

    async def public_for_listing(
        self, tenant: TenantContext, listing_id: uuid.UUID
    ) -> list[ListingMedia]:
        return await self.repo.list_public_for_listing(tenant.id, listing_id)

    async def covers_for(
        self, tenant: TenantContext, listing_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, ListingMedia]:
        return await self.repo.covers_for_listings(tenant.id, listing_ids)

    def public_url(self, key: str) -> str:
        return self.storage.public_url(key)

    async def photo_urls_for(
        self, tenant: TenantContext, listing_id: uuid.UUID, *, limit: int = 20
    ) -> list[str]:
        """Public photo URLs (largest available variant, cover/position order)
        for a published listing — boundary accessor for the syndication feed and
        portal payloads (§8.14). Only ``ready`` photos with variants qualify."""
        rows = await self.repo.list_public_for_listing(tenant.id, listing_id)
        urls: list[str] = []
        for media in rows:
            if media.kind is not MediaKind.PHOTO or not media.variants:
                continue
            variant = (
                media.variants.get("full_webp")
                or media.variants.get("gallery_webp")
                or media.variants.get("full_jpeg")
                or next(iter(media.variants.values()), None)
            )
            if variant is not None:
                urls.append(self.storage.public_url(variant["key"]))
            if len(urls) >= limit:
                break
        return urls


def get_media_service(session: SessionDep, request: Request) -> MediaService:
    return MediaService(
        MediaRepository(session),
        get_listing_service(session),
        request.app.state.storage,
        request.app.state.settings,
    )


def build_media_boundary(
    session: AsyncSession, storage: ObjectStorage, settings: Settings
) -> MediaService:
    """Construct a :class:`MediaService` for dependents (syndication §8.14) that
    need its public-URL boundary without pulling ``request`` into their factory."""
    return MediaService(MediaRepository(session), get_listing_service(session), storage, settings)


MediaServiceDep = Annotated[MediaService, Depends(get_media_service)]
