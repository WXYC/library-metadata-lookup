"""Object-store abstraction for LML's two hosted SQLite artifacts (WXYC#835).

An :class:`ObjectStore` is a tiny key -> blob interface over either a Railway
Bucket (:class:`S3ObjectStore`, S3-compatible) or a local directory
(:class:`LocalDirStore`, for dev + the existing endpoint test suite). Exactly one
implementation is active per deployment, selected in ``core/dependencies.py`` by
whether the bucket settings are present.

Storage operations here are *rare* — one boot fetch of ``library.db``, one daily
upload, and weekly ``streaming_availability.db`` round-trips — so
:class:`S3ObjectStore` uses the plain synchronous ``boto3`` client wrapped in
``asyncio.to_thread`` rather than an async-native client (locked in the design
review: ``aioboto3``'s ``aiobotocore`` version-pinning fragility isn't worth it
for this call volume). See the volume-eviction epic
(WXYC/library-metadata-lookup#834), PR 1.

This module is deliberately **inert** in PR 1 — nothing calls the store yet. PR 2
(streaming endpoints) and PR 3 (``library.db`` boot-fetch + upload) consume it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class ObjectNotFoundError(Exception):
    """Raised by :meth:`ObjectStore.get` when the key has no object.

    A uniform, backend-independent signal so callers branch on absence the same
    way against S3 (a ``NoSuchKey`` ``ClientError``) and the local directory (a
    ``FileNotFoundError``). Distinct from :meth:`ObjectStore.head` /
    :meth:`ObjectStore.exists`, which report absence as ``None`` / ``False``
    rather than raising.
    """


@dataclass(frozen=True)
class ObjectStat:
    """Metadata for a stored object.

    ``etag`` is populated by S3 (a content hash, useful for the cutover
    checksum-verify step) and ``None`` for the local store, which does not
    compute one.
    """

    size: int
    etag: str | None = None


@runtime_checkable
class ObjectStore(Protocol):
    """Minimal async key -> blob store: ``get`` / ``put`` / ``exists`` / ``head``.

    All methods are async so the S3 implementation can offload blocking boto3
    calls to a thread without changing the call sites.
    """

    async def get(self, key: str) -> bytes:
        """Return the object's bytes, or raise :class:`ObjectNotFoundError`."""
        ...

    async def put(self, key: str, data: bytes | Path) -> None:
        """Store ``data`` (raw bytes or a source file path) under ``key``."""
        ...

    async def exists(self, key: str) -> bool:
        """Return whether an object exists under ``key``."""
        ...

    async def head(self, key: str) -> ObjectStat | None:
        """Return the object's :class:`ObjectStat`, or ``None`` if absent."""
        ...


def _s3_error_is_not_found(err: ClientError) -> bool:
    """Whether a boto3 ``ClientError`` means "no such object" (404)."""
    response = getattr(err, "response", {}) or {}
    code = response.get("Error", {}).get("Code")
    if code in {"404", "NoSuchKey", "NotFound"}:
        return True
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status == 404


class S3ObjectStore:
    """:class:`ObjectStore` over an S3-compatible bucket (Railway Bucket).

    Credentials are read by boto3 natively from the standard
    ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` environment variables
    (Railway's bucket variable-reference presets provision these names), so they
    are not passed explicitly. The client is built once and reused; boto3
    low-level clients are safe to call across threads, which is what
    ``asyncio.to_thread`` does here.
    """

    def __init__(
        self,
        bucket: str,
        endpoint_url: str,
        *,
        region: str = "us-east-1",
        connect_timeout: float = 5.0,
        read_timeout: float = 60.0,
        max_attempts: int = 3,
    ) -> None:
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        config = Config(
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            retries={"max_attempts": max_attempts, "mode": "standard"},
        )
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            config=config,
        )

    async def get(self, key: str) -> bytes:
        def _get() -> bytes:
            try:
                resp = self._client.get_object(Bucket=self.bucket, Key=key)
            except ClientError as err:
                if _s3_error_is_not_found(err):
                    raise ObjectNotFoundError(key) from err
                raise
            body = resp["Body"]
            try:
                data: bytes = body.read()
            finally:
                body.close()
            return data

        return await asyncio.to_thread(_get)

    async def put(self, key: str, data: bytes | Path) -> None:
        def _put() -> None:
            if isinstance(data, Path):
                # upload_file streams from disk (multipart for large files), so a
                # 53MB streaming DB never lands wholly in memory.
                self._client.upload_file(str(data), self.bucket, key)
            else:
                self._client.put_object(Bucket=self.bucket, Key=key, Body=data)

        await asyncio.to_thread(_put)

    async def head(self, key: str) -> ObjectStat | None:
        def _head() -> ObjectStat | None:
            try:
                resp = self._client.head_object(Bucket=self.bucket, Key=key)
            except ClientError as err:
                if _s3_error_is_not_found(err):
                    return None
                raise
            return ObjectStat(size=resp["ContentLength"], etag=resp.get("ETag"))

        return await asyncio.to_thread(_head)

    async def exists(self, key: str) -> bool:
        return (await self.head(key)) is not None


class LocalDirStore:
    """:class:`ObjectStore` over a local directory (dev + endpoint test suite).

    Keys must be bare filenames; a key with a directory component (``a/b``,
    ``../escape``) raises ``ValueError`` rather than escaping the base directory.
    Writes go through a temp file + ``os.replace`` so a reader never observes a
    half-written object.
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)

    def _resolve(self, key: str) -> Path:
        if not key or Path(key).name != key:
            raise ValueError(f"object key must be a bare filename, got {key!r}")
        return self.base_dir / key

    async def get(self, key: str) -> bytes:
        path = self._resolve(key)

        def _get() -> bytes:
            try:
                return path.read_bytes()
            except FileNotFoundError as err:
                raise ObjectNotFoundError(key) from err

        return await asyncio.to_thread(_get)

    async def put(self, key: str, data: bytes | Path) -> None:
        dest = self._resolve(key)

        def _put() -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            try:
                if isinstance(data, Path):
                    tmp.write_bytes(data.read_bytes())
                else:
                    tmp.write_bytes(data)
                os.replace(tmp, dest)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise

        await asyncio.to_thread(_put)

    async def head(self, key: str) -> ObjectStat | None:
        path = self._resolve(key)

        def _head() -> ObjectStat | None:
            try:
                return ObjectStat(size=path.stat().st_size, etag=None)
            except FileNotFoundError:
                return None

        return await asyncio.to_thread(_head)

    async def exists(self, key: str) -> bool:
        return (await self.head(key)) is not None
