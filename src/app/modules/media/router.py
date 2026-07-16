"""HTTP layer for listing media (§8.2). All portal routes; the public site
reads media through the listings public endpoints, never here.

Upload contract: ``POST .../media/uploads`` → presigned PUT (the file goes
straight to storage, §8.2) → ``POST /portal/media/{id}/confirm`` → the
pipeline flips the row to ``ready``/``failed`` — clients poll the media list
or just re-render when the listing is next viewed.
"""

import uuid

from fastapi import APIRouter, Depends, status

from app.core.permissions import AuthenticatedUser, Permission, require
from app.core.tenancy import TenantDep
from app.modules.media.schemas import (
    MediaDownloadOut,
    MediaEmbedCreate,
    MediaOut,
    MediaUpdate,
    MediaUploadOut,
    MediaUploadRequest,
)
from app.modules.media.service import MediaServiceDep

router = APIRouter(prefix="/portal", tags=["media:portal"])


@router.post("/listings/{listing_id}/media/uploads", status_code=status.HTTP_201_CREATED)
async def request_upload(
    listing_id: uuid.UUID,
    data: MediaUploadRequest,
    tenant: TenantDep,
    service: MediaServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> MediaUploadOut:
    media, upload_url, headers = await service.request_upload(tenant, actor, listing_id, data)
    return MediaUploadOut(
        media=MediaOut.from_media(media, service.public_url),
        upload_url=upload_url,
        upload_headers=headers,
        expires_in_seconds=service.settings.media_upload_url_ttl_seconds,
    )


@router.post("/listings/{listing_id}/media/embeds", status_code=status.HTTP_201_CREATED)
async def create_embed(
    listing_id: uuid.UUID,
    data: MediaEmbedCreate,
    tenant: TenantDep,
    service: MediaServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> MediaOut:
    media = await service.create_embed(tenant, actor, listing_id, data)
    return MediaOut.from_media(media, service.public_url)


@router.get("/listings/{listing_id}/media")
async def list_media(
    listing_id: uuid.UUID,
    tenant: TenantDep,
    service: MediaServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> list[MediaOut]:
    rows = await service.list_for_listing(tenant, actor, listing_id)
    return [MediaOut.from_media(m, service.public_url) for m in rows]


@router.post("/media/{media_id}/confirm", status_code=status.HTTP_202_ACCEPTED)
async def confirm_upload(
    media_id: uuid.UUID,
    tenant: TenantDep,
    service: MediaServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> MediaOut:
    media = await service.confirm_upload(tenant, actor, media_id)
    return MediaOut.from_media(media, service.public_url)


@router.patch("/media/{media_id}")
async def update_media(
    media_id: uuid.UUID,
    data: MediaUpdate,
    tenant: TenantDep,
    service: MediaServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> MediaOut:
    media = await service.update(tenant, actor, media_id, data)
    return MediaOut.from_media(media, service.public_url)


@router.delete("/media/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    media_id: uuid.UUID,
    tenant: TenantDep,
    service: MediaServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> None:
    await service.delete(tenant, actor, media_id)


@router.get("/media/{media_id}/download")
async def download_media(
    media_id: uuid.UUID,
    tenant: TenantDep,
    service: MediaServiceDep,
    actor: AuthenticatedUser = Depends(require(Permission.LISTING_MANAGE)),
) -> MediaDownloadOut:
    url = await service.download_url(tenant, actor, media_id)
    return MediaDownloadOut(
        download_url=url,
        expires_in_seconds=service.settings.media_download_url_ttl_seconds,
    )
