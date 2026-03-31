"""MinioObjectStore - ObjectStore implementation backed by MinIO."""

from __future__ import annotations

import logging
from io import BytesIO

from semcore.providers.base import ObjectStore

log = logging.getLogger(__name__)


class MinioObjectStore(ObjectStore):
    """MinIO-backed object store."""

    def __init__(self, settings: object | None = None) -> None:
        if settings is None:
            from src.config.settings import settings as _s
            settings = _s

        self._endpoint = settings.MINIO_ENDPOINT
        self._access_key = settings.MINIO_ACCESS_KEY
        self._secret_key = settings.MINIO_SECRET_KEY
        self._secure = bool(settings.MINIO_SECURE)
        self._default_bucket = settings.MINIO_BUCKET_RAW
        self._cleaned_bucket = settings.MINIO_BUCKET_CLEANED
        self._buckets = {settings.MINIO_BUCKET_RAW, settings.MINIO_BUCKET_CLEANED}

        try:
            from minio import Minio
        except ImportError as exc:
            log.error("minio SDK not installed: %s", exc)
            raise

        self._client = Minio(
            self._endpoint,
            access_key=self._access_key,
            secret_key=self._secret_key,
            secure=self._secure,
        )

        self._ensure_buckets()
        log.info("MinIO client ready: %s (secure=%s)", self._endpoint, self._secure)

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        bucket, object_name = self._split_key(key)
        bio = BytesIO(data)
        self._client.put_object(
            bucket,
            object_name,
            bio,
            length=len(data),
            content_type=content_type,
        )
        return f"minio://{bucket}/{object_name}"

    def get(self, uri: str) -> bytes:
        bucket, object_name = self._split_uri(uri)
        try:
            resp = self._client.get_object(bucket, object_name)
            return resp.read()
        finally:
            try:
                resp.close()
                resp.release_conn()
            except Exception:
                pass

    def exists(self, uri: str) -> bool:
        bucket, object_name = self._split_uri(uri)
        try:
            self._client.stat_object(bucket, object_name)
            return True
        except Exception:
            return False

    def _ensure_buckets(self) -> None:
        for bucket in sorted(self._buckets):
            if not bucket:
                continue
            try:
                if not self._client.bucket_exists(bucket):
                    self._client.make_bucket(bucket)
                    log.info("MinIO bucket created: %s", bucket)
                else:
                    log.info("MinIO bucket exists: %s", bucket)
            except Exception as exc:
                log.error("MinIO bucket check failed for %s: %s", bucket, exc)
                raise

    def _split_uri(self, uri: str) -> tuple[str, str]:
        if uri.startswith("minio://"):
            _, rest = uri.split("minio://", 1)
            parts = rest.split("/", 1)
            if len(parts) == 2:
                return parts[0], parts[1]
        return self._split_key(uri)

    def _split_key(self, key: str) -> tuple[str, str]:
        key = key.lstrip("/")
        if key.startswith("minio://"):
            return self._split_uri(key)
        parts = key.split("/", 1)
        if len(parts) == 2 and parts[0] in self._buckets:
            return parts[0], parts[1]
        # Map key prefix to the appropriate bucket
        if key.startswith("cleaned/"):
            return self._cleaned_bucket, key[len("cleaned/"):]
        if key.startswith("raw/"):
            return self._default_bucket, key[len("raw/"):]
        return self._default_bucket, key
