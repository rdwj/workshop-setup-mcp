"""Pluggable bytes-storage backends for the file-upload pipeline.

Per ADR-0001, file metadata and file bytes are split: ``FileStore``
implementations (``SqliteFileStore``, ``PostgresFileStore``) own the
metadata path; ``BytesStore`` implementations own the raw bytes. This
lets the same metadata backend (e.g. Postgres) compose with any bytes
target (local FS for dev, S3-compatible for production).

The keyspace is ``file_id`` everywhere — there is no user-supplied
filename in any storage path.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


def _shard_key(file_id: str) -> str:
    """``file_<hex>`` → ``fi/file_<hex>`` — 2-char prefix shard.

    Used by every backend so the keyspace layout is identical across
    LocalFs and S3. Lets operators bulk-copy a local ``bytes_dir`` to a
    bucket without rewriting keys.
    """
    shard = file_id[:2] if len(file_id) >= 2 else "00"
    return f"{shard}/{file_id}"


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class BytesStore(ABC):
    """Abstract storage for raw file bytes.

    Implementations key by ``file_id`` (a ``file_<32 hex>`` UUID-derived
    identifier). Layout is implementation-defined but every backend
    uses :func:`_shard_key` so a local ``bytes_dir`` can be migrated to
    S3 with ``aws s3 sync`` without rewriting paths.
    """

    @abstractmethod
    async def put(
        self,
        file_id: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        """Store *data* under *file_id*. Idempotent — last write wins."""

    @abstractmethod
    async def get(self, file_id: str) -> bytes | None:
        """Return the bytes stored under *file_id*, or ``None`` if absent."""

    @abstractmethod
    async def delete(self, file_id: str) -> bool:
        """Remove the bytes stored under *file_id*. Returns whether the
        object existed."""

    async def close(self) -> None:
        """Release any held resources. Default no-op."""
        return None


# ---------------------------------------------------------------------------
# Null (accepts then drops)
# ---------------------------------------------------------------------------


class NullBytesStore(BytesStore):
    """Bytes go nowhere. Used in tests and the metadata-only ``Null``
    composition."""

    async def put(
        self,
        file_id: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        return None

    async def get(self, file_id: str) -> bytes | None:
        return None

    async def delete(self, file_id: str) -> bool:
        return False


# ---------------------------------------------------------------------------
# Local filesystem
# ---------------------------------------------------------------------------


class LocalFsBytesStore(BytesStore):
    """Sharded local-filesystem bytes.

    Layout: ``<bytes_dir>/fi/file_<hex>``. Two-character prefix shard
    keeps a single directory from growing unbounded. The shard prefix
    is derived from ``file_id`` — every backend uses the same scheme so
    bulk-copy migrations preserve keys.

    Writes are atomic via ``write-temp + os.replace``. Deletes
    best-effort-cleanup the shard dir when it becomes empty.
    """

    def __init__(self, bytes_dir: str) -> None:
        self._bytes_dir = bytes_dir
        self._initialized = False

    def _path(self, file_id: str) -> str:
        return os.path.join(self._bytes_dir, _shard_key(file_id))

    def _ensure_root(self) -> None:
        if not self._initialized:
            os.makedirs(self._bytes_dir, exist_ok=True)
            self._initialized = True

    async def put(
        self,
        file_id: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        self._ensure_root()
        path = self._path(file_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)

    async def get(self, file_id: str) -> bytes | None:
        path = self._path(file_id)
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    async def delete(self, file_id: str) -> bool:
        path = self._path(file_id)
        try:
            os.remove(path)
        except FileNotFoundError:
            return False
        # Best-effort: drop the shard dir if it is now empty.
        shard_dir = os.path.dirname(path)
        try:
            if shard_dir and shard_dir != self._bytes_dir:
                os.rmdir(shard_dir)
        except OSError:
            pass
        return True


# ---------------------------------------------------------------------------
# S3-compatible
# ---------------------------------------------------------------------------


class S3BytesStore(BytesStore):
    """S3-compatible object storage (AWS S3, MinIO, GCS S3-mode, R2, B2).

    Backed by ``aioboto3`` (async wrapper over boto3). Optional extra:
    ``pip install fipsagents[s3]``. The session and resource clients
    are created lazily on first use so import-time cost is zero when
    the store isn't wired in.

    Authentication: explicit ``access_key`` / ``secret_key`` if set,
    otherwise boto3's default chain (env vars, IAM role, EC2 metadata,
    etc.). Empty/None falls through to the chain.

    MinIO and other path-style endpoints require ``path_style=True``.
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint: str | None = None,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
        prefix: str = "",
        path_style: bool = False,
    ) -> None:
        if not bucket:
            raise ValueError("S3BytesStore requires a non-empty bucket")
        self._bucket = bucket
        self._endpoint = endpoint or None
        self._region = region
        self._access_key = access_key or None
        self._secret_key = secret_key or None
        # Normalize prefix so we can join without worrying about leading
        # or trailing slashes — the canonical key is ``<prefix>/fi/<id>``.
        self._prefix = prefix.strip("/")
        self._path_style = path_style
        self._session: Any = None
        self._client_cm: Any = None
        self._client: Any = None

    def _key(self, file_id: str) -> str:
        suffix = _shard_key(file_id)
        return f"{self._prefix}/{suffix}" if self._prefix else suffix

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import aioboto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "S3BytesStore requires the [s3] extra: "
                "pip install fipsagents[s3]"
            ) from exc

        boto_config = BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path" if self._path_style else "auto"},
        )
        self._session = aioboto3.Session()
        # Lazily build kwargs so explicit creds override the default
        # chain only when set.
        kwargs: dict[str, Any] = {
            "service_name": "s3",
            "region_name": self._region,
            "config": boto_config,
        }
        if self._endpoint:
            kwargs["endpoint_url"] = self._endpoint
        if self._access_key and self._secret_key:
            kwargs["aws_access_key_id"] = self._access_key
            kwargs["aws_secret_access_key"] = self._secret_key
        self._client_cm = self._session.client(**kwargs)
        self._client = await self._client_cm.__aenter__()
        return self._client

    async def put(
        self,
        file_id: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        client = await self._get_client()
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": self._key(file_id),
            "Body": data,
        }
        if content_type:
            kwargs["ContentType"] = content_type
        await client.put_object(**kwargs)

    async def get(self, file_id: str) -> bytes | None:
        client = await self._get_client()
        try:
            resp = await client.get_object(
                Bucket=self._bucket, Key=self._key(file_id),
            )
        except Exception as exc:
            # Surface 404 (NoSuchKey) as None; everything else
            # re-raises. ClientError carries a Code attribute we can
            # check without depending on botocore at module top-level.
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404"}:
                return None
            raise
        body = resp["Body"]
        try:
            return await body.read()
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                maybe = close()
                if hasattr(maybe, "__await__"):
                    await maybe

    async def delete(self, file_id: str) -> bool:
        client = await self._get_client()
        # S3 delete_object is idempotent; we have to head_object first
        # to know whether the key existed before the delete (matches
        # LocalFsBytesStore semantics).
        try:
            await client.head_object(
                Bucket=self._bucket, Key=self._key(file_id),
            )
            existed = True
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "NotFound"}:
                existed = False
            else:
                raise
        if existed:
            await client.delete_object(
                Bucket=self._bucket, Key=self._key(file_id),
            )
        return existed

    async def close(self) -> None:
        if self._client_cm is not None:
            try:
                await self._client_cm.__aexit__(None, None, None)
            except Exception:
                logger.warning(
                    "S3BytesStore: error closing client", exc_info=True,
                )
            finally:
                self._client = None
                self._client_cm = None
                self._session = None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_bytes_store(
    backend_type: str | None,
    *,
    bytes_dir: str = "./files",
    s3_bucket: str = "",
    s3_endpoint: str | None = None,
    s3_region: str = "us-east-1",
    s3_access_key: str | None = None,
    s3_secret_key: str | None = None,
    s3_prefix: str = "",
    s3_path_style: bool = False,
) -> BytesStore:
    """Build a ``BytesStore`` from config values.

    *backend_type* dispatches between implementations:
      - ``"local_fs"`` (or empty / None) — :class:`LocalFsBytesStore`
      - ``"s3"`` — :class:`S3BytesStore`
      - ``"null"`` — :class:`NullBytesStore`

    The S3 args are ignored unless ``backend_type == "s3"``.
    """
    if backend_type in (None, "", "local_fs"):
        return LocalFsBytesStore(bytes_dir)
    if backend_type == "null":
        return NullBytesStore()
    if backend_type == "s3":
        if not s3_bucket:
            raise ValueError(
                "bytes_backend.type='s3' requires a non-empty bucket",
            )
        return S3BytesStore(
            bucket=s3_bucket,
            endpoint=s3_endpoint,
            region=s3_region,
            access_key=s3_access_key,
            secret_key=s3_secret_key,
            prefix=s3_prefix,
            path_style=s3_path_style,
        )
    raise ValueError(
        f"unknown bytes_backend type: {backend_type!r} "
        f"(expected 'local_fs', 's3', or 'null')"
    )
