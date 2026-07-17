"""Unit tests for storage/object_store.py (WXYC/library-metadata-lookup#835).

The two ObjectStore implementations — S3ObjectStore (moto-mocked, in-process)
and LocalDirStore (tmp_path) — are exercised against one shared contract via a
parametrized ``store`` fixture, then each gets a couple of impl-specific tests.
moto patches botocore's HTTP layer, so no network or real service is touched and
no pytest marker is needed.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from storage.object_store import (
    LocalDirStore,
    ObjectNotFoundError,
    ObjectStore,
    S3ObjectStore,
)

BUCKET = "lml-test-bucket"
# moto's decorator only intercepts AWS-recognized endpoints; the store passes
# endpoint_url straight through to boto3 with no endpoint-specific logic, so an
# AWS endpoint exercises the same get/put/head/exists code the Railway Bucket
# endpoint would in prod.
ENDPOINT = "https://s3.amazonaws.com"


@pytest.fixture
def local_store(tmp_path: Path) -> LocalDirStore:
    return LocalDirStore(base_dir=tmp_path)


@pytest.fixture
def s3_store(monkeypatch):
    """An S3ObjectStore backed by an in-process moto S3 with the bucket created.

    The ``mock_aws`` context wraps the ``yield`` so it stays active for the whole
    test body, not just fixture setup — the store's own boto3 calls in the test
    are intercepted too.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield S3ObjectStore(bucket=BUCKET, endpoint_url=ENDPOINT, region="us-east-1")


@pytest.fixture(params=["local", "s3"])
def store(request):
    """Parametrized over both implementations to pin the shared contract."""
    return request.getfixturevalue(f"{request.param}_store")


class TestObjectStoreContract:
    """Behaviors both implementations must satisfy identically."""

    @pytest.mark.asyncio
    async def test_put_bytes_then_get(self, store):
        await store.put("library.db", b"sqlite-bytes")
        assert await store.get("library.db") == b"sqlite-bytes"

    @pytest.mark.asyncio
    async def test_put_path_then_get(self, store, tmp_path):
        src = tmp_path / "src.db"
        src.write_bytes(b"from-a-file")
        await store.put("streaming_availability.db", src)
        assert await store.get("streaming_availability.db") == b"from-a-file"

    @pytest.mark.asyncio
    async def test_exists_false_then_true(self, store):
        assert await store.exists("library.db") is False
        await store.put("library.db", b"x")
        assert await store.exists("library.db") is True

    @pytest.mark.asyncio
    async def test_head_missing_is_none(self, store):
        assert await store.head("nope.db") is None

    @pytest.mark.asyncio
    async def test_head_reports_size(self, store):
        await store.put("library.db", b"12345")
        stat = await store.head("library.db")
        assert stat is not None
        assert stat.size == 5

    @pytest.mark.asyncio
    async def test_get_missing_raises_object_not_found(self, store):
        with pytest.raises(ObjectNotFoundError):
            await store.get("missing.db")

    @pytest.mark.asyncio
    async def test_put_overwrites(self, store):
        await store.put("library.db", b"old")
        await store.put("library.db", b"newer-and-longer")
        assert await store.get("library.db") == b"newer-and-longer"
        stat = await store.head("library.db")
        assert stat is not None
        assert stat.size == len(b"newer-and-longer")

    def test_satisfies_runtime_protocol(self, store):
        assert isinstance(store, ObjectStore)


class TestLocalDirStore:
    @pytest.mark.asyncio
    async def test_put_leaves_no_tmp_file(self, local_store, tmp_path):
        """The atomic write-then-replace must not strand a ``.tmp`` sibling."""
        await local_store.put("library.db", b"data")
        assert [p.name for p in tmp_path.iterdir()] == ["library.db"]

    @pytest.mark.asyncio
    async def test_put_creates_missing_base_dir(self, tmp_path):
        nested = tmp_path / "not-yet"
        store = LocalDirStore(base_dir=nested)
        await store.put("library.db", b"data")
        assert (nested / "library.db").read_bytes() == b"data"

    @pytest.mark.asyncio
    async def test_key_with_directory_component_rejected(self, local_store):
        """Keys must be bare filenames — a traversal attempt raises, not escapes."""
        with pytest.raises(ValueError):
            await local_store.get("../escape.db")

    def test_exposes_base_dir(self, tmp_path):
        assert LocalDirStore(base_dir=tmp_path).base_dir == tmp_path


class TestS3ObjectStore:
    @pytest.mark.asyncio
    async def test_head_returns_etag(self, s3_store):
        await s3_store.put("library.db", b"data")
        stat = await s3_store.head("library.db")
        assert stat is not None
        assert stat.etag  # S3 always returns an ETag

    @pytest.mark.asyncio
    async def test_put_path_roundtrip(self, s3_store, tmp_path):
        src = tmp_path / "big.db"
        src.write_bytes(b"x" * 4096)
        await s3_store.put("library.db", src)
        assert await s3_store.get("library.db") == b"x" * 4096

    def test_exposes_bucket(self):
        store = S3ObjectStore(bucket="lml-prod", endpoint_url=ENDPOINT, region="us-east-1")
        assert store.bucket == "lml-prod"

    def test_uses_path_style_addressing(self):
        """Custom S3-compatible endpoints (Railway Bucket) need path-style; boto3's
        ``auto`` default would try virtual-host and fail on absent wildcard DNS."""
        store = S3ObjectStore(bucket="lml-prod", endpoint_url=ENDPOINT, region="us-east-1")
        assert store._client.meta.config.s3["addressing_style"] == "path"

    @pytest.mark.asyncio
    async def test_get_propagates_non_404_client_error(self, s3_store, monkeypatch):
        """A non-absence error (e.g. 403) must surface, not be masked as a miss."""
        forbidden = ClientError(
            {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
            "GetObject",
        )

        def _boom(*args, **kwargs):
            raise forbidden

        monkeypatch.setattr(s3_store._client, "get_object", _boom)
        with pytest.raises(ClientError):
            await s3_store.get("library.db")
