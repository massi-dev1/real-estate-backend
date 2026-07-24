"""S3-compatible object storage (§8.2) — MinIO locally, any S3 API in prod.

Uploads never pass through FastAPI: the API hands the client a presigned PUT
and the file goes straight to storage. Presigning is pure local computation
(no network round-trip), so the sync boto3 client is safe to call from async
routes; the methods that *do* talk to storage (get/put/delete) run only in
Celery task bodies.
"""

from typing import TYPE_CHECKING

import boto3
from botocore.config import Config as BotoConfig

from app.core.config import Settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


class ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: S3Client = boto3.client(
            "s3",
            endpoint_url=settings.storage_endpoint_url,
            aws_access_key_id=settings.storage_access_key,
            aws_secret_access_key=settings.storage_secret_key,
            region_name=settings.storage_region,
            # Path-style: bucket in the path, not the hostname — required for
            # MinIO and keeps presigned URLs valid without wildcard DNS.
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        self.media_bucket = settings.storage_media_bucket
        self.docs_bucket = settings.storage_docs_bucket

    # ---- presigning (local computation, async-route safe) ----

    def presign_put(self, bucket: str, key: str, content_type: str) -> str:
        """Presigned PUT pinned to a Content-Type; the client must send the
        same header or the signature fails. Size is enforced post-upload by
        the processing task (PUT signatures cannot cap Content-Length)."""
        return self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=self._settings.media_upload_url_ttl_seconds,
        )

    def presign_get(self, bucket: str, key: str, *, filename: str | None = None) -> str:
        params: dict[str, str] = {"Bucket": bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return self._client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=self._settings.media_download_url_ttl_seconds,
        )

    def public_url(self, key: str) -> str:
        """URL a browser fetches a public variant from — the CDN base when
        configured, the storage endpoint (anonymous-download bucket) locally."""
        base = self._settings.media_public_base_url.rstrip("/")
        if base:
            return f"{base}/{key}"
        return f"{self._settings.storage_endpoint_url.rstrip('/')}/{self.media_bucket}/{key}"

    def bucket_reachable(self) -> bool:
        """HEAD the media bucket — the cheapest liveness probe the S3 API
        offers. Blocking network I/O, so async callers must run it in a thread
        (``/readyz`` does). Never raises: a probe reports, it does not fail."""
        try:
            self._client.head_bucket(Bucket=self.media_bucket)
        except Exception:
            return False
        return True

    # ---- object I/O (network — Celery task bodies only) ----

    def object_size(self, bucket: str, key: str) -> int:
        """HEAD the object — lets callers reject oversized uploads *before*
        buffering the body (a presigned PUT cannot cap Content-Length)."""
        return self._client.head_object(Bucket=bucket, Key=key)["ContentLength"]

    def get_object(self, bucket: str, key: str) -> bytes:
        return self._client.get_object(Bucket=bucket, Key=key)["Body"].read()

    def put_object(self, bucket: str, key: str, body: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            # Content-hashed keys never change content — cache forever (§8.2).
            CacheControl="public, max-age=31536000, immutable",
        )

    def delete_objects(self, bucket: str, keys: list[str]) -> None:
        if not keys:
            return
        # delete_objects caps at 1000 keys per call.
        for start in range(0, len(keys), 1000):
            chunk = keys[start : start + 1000]
            self._client.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True}
            )


def create_storage(settings: Settings) -> ObjectStorage:
    return ObjectStorage(settings)
