"""Bucket-mode tests for the /admin streaming-DB endpoints (WXYC#836).

Pin the LML#672 coverage-guard semantics when the active store is an
:class:`S3ObjectStore` (Railway Bucket) rather than the local volume: a missing
object is accepted (first upload), an unreadable object is refused with 409, a
coverage regression is refused with 409, and ``force=true`` overrides each. The
download endpoint streams the object and 404s when the key is absent.

moto (``mock_aws``) patches botocore's HTTP layer in-process, so no network or
real bucket is touched and no pytest marker is needed — the same approach the
#835 store unit tests use. The existing local-mode suite
(``test_admin_streaming_guard.py``) proves the identical contract against
``LocalDirStore``; the two together are the "same behavior in both store modes"
guarantee the epic's drift-risk mitigation calls for.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import boto3
import pytest
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

from config.settings import Settings, get_settings
from core.dependencies import (
    get_discogs_service,
    get_library_db,
    get_object_store,
    get_posthog_client,
)
from routers.admin import STREAMING_DB_FILENAME
from storage.object_store import ObjectNotFoundError, S3ObjectStore
from tests.unit.conftest import override_deps
from tests.unit.test_admin_streaming_guard import _make_streaming_db

BUCKET = "lml-streaming-test"
# moto's decorator only intercepts AWS-recognized endpoints; the store passes
# endpoint_url straight through to boto3 with no endpoint-specific logic, so an
# AWS endpoint exercises the same code the Railway Bucket endpoint would in prod.
ENDPOINT = "https://s3.amazonaws.com"


@pytest.fixture
def admin_settings(tmp_path):
    return Settings(
        admin_token="bucket-token",
        library_db_path=tmp_path / "library.db",
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
    )


@pytest.fixture
def s3_store(monkeypatch):
    """An :class:`S3ObjectStore` backed by an in-process moto S3 with the bucket made.

    ``mock_aws`` wraps the ``yield`` so it stays active for the whole test body —
    the endpoint's own boto3 get/put (run in ``asyncio.to_thread``) is intercepted
    too, not just fixture setup.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield S3ObjectStore(bucket=BUCKET, endpoint_url=ENDPOINT, region="us-east-1")


async def _seed(store: S3ObjectStore, tmp_path: Path, **coverage) -> None:
    """Write a streaming DB with the given coverage into the store under the key."""
    seed = tmp_path / "seed.db"
    _make_streaming_db(seed, **coverage)
    await store.put(STREAMING_DB_FILENAME, seed)


async def _apple_count_in_store(store: S3ObjectStore, tmp_path: Path) -> int:
    """COUNT(apple_url) of the DB currently stored under the streaming key."""
    data = await store.get(STREAMING_DB_FILENAME)
    check = tmp_path / "check.db"
    check.write_bytes(data)
    conn = sqlite3.connect(str(check))
    try:
        return conn.execute("SELECT COUNT(apple_url) FROM albums").fetchone()[0]
    finally:
        conn.close()


async def _upload(app, settings, store, db_file, *, force=False):
    url = "/admin/upload-streaming-db" + ("?force=true" if force else "")
    with override_deps(
        app,
        {
            get_library_db: AsyncMock(),
            get_discogs_service: None,
            get_posthog_client: None,
            get_settings: settings,
            get_object_store: store,
        },
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            with open(db_file, "rb") as f:
                return await client.post(
                    url,
                    headers={"Authorization": "Bearer bucket-token"},
                    files={"file": ("streaming_availability.db", f, "application/octet-stream")},
                )


class TestUploadBucketGuard:
    @pytest.mark.asyncio
    async def test_first_upload_missing_object_accepted(self, tmp_path, admin_settings, s3_store):
        """No object under the key -> accepted like a local first upload."""
        from main import app

        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=10, apple=5, spotify=8, deezer=6, track_results=20)

        resp = await _upload(app, admin_settings, s3_store, upload)
        assert resp.status_code == 200
        assert resp.json()["row_count"] == 10
        assert await s3_store.exists(STREAMING_DB_FILENAME) is True

    @pytest.mark.asyncio
    async def test_growth_allowed(self, tmp_path, admin_settings, s3_store):
        from main import app

        await _seed(s3_store, tmp_path, albums=100, apple=50, spotify=80, deezer=60)
        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=110, apple=60, spotify=90, deezer=70)

        resp = await _upload(app, admin_settings, s3_store, upload)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_regression_rejected(self, tmp_path, admin_settings, s3_store):
        """The 288 -> 0 incident against a seeded bucket object -> 409, object untouched."""
        from main import app

        await _seed(s3_store, tmp_path, albums=300, apple=288, spotify=200, deezer=150)
        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=300, apple=0, spotify=200, deezer=150)

        resp = await _upload(app, admin_settings, s3_store, upload)
        assert resp.status_code == 409
        regs = resp.json()["detail"]["regressions"]
        assert any(r["metric"] == "apple_url" for r in regs)
        # The object in the bucket is left untouched.
        assert await _apple_count_in_store(s3_store, tmp_path) == 288

    @pytest.mark.asyncio
    async def test_force_overrides_regression(self, tmp_path, admin_settings, s3_store):
        from main import app

        await _seed(s3_store, tmp_path, albums=300, apple=288, spotify=200, deezer=150)
        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=300, apple=0, spotify=200, deezer=150)

        resp = await _upload(app, admin_settings, s3_store, upload, force=True)
        assert resp.status_code == 200
        assert await _apple_count_in_store(s3_store, tmp_path) == 0

    @pytest.mark.asyncio
    async def test_unreadable_object_rejected_not_fail_open(
        self, tmp_path, admin_settings, s3_store
    ):
        """A present-but-corrupt object fails closed (409), not accepted as a first upload."""
        from main import app

        await s3_store.put(STREAMING_DB_FILENAME, b"this is not a sqlite database")
        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=300, apple=0, spotify=200, deezer=150)

        resp = await _upload(app, admin_settings, s3_store, upload)
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "error" in detail
        assert "regressions" not in detail
        # The corrupt object is left untouched.
        assert await s3_store.get(STREAMING_DB_FILENAME) == b"this is not a sqlite database"

    @pytest.mark.asyncio
    async def test_force_overrides_unreadable_object(self, tmp_path, admin_settings, s3_store):
        from main import app

        await s3_store.put(STREAMING_DB_FILENAME, b"this is not a sqlite database")
        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=300, apple=288, spotify=200, deezer=150)

        resp = await _upload(app, admin_settings, s3_store, upload, force=True)
        assert resp.status_code == 200
        assert await _apple_count_in_store(s3_store, tmp_path) == 288


class TestDownloadBucket:
    async def _download(self, app, settings, store):
        with override_deps(
            app,
            {
                get_library_db: AsyncMock(),
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: settings,
                get_object_store: store,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                return await client.get(
                    "/admin/download-streaming-db",
                    headers={"Authorization": "Bearer bucket-token"},
                )

    @pytest.mark.asyncio
    async def test_download_missing_object_404(self, admin_settings, s3_store):
        from main import app

        resp = await self._download(app, admin_settings, s3_store)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_download_returns_object_bytes(self, tmp_path, admin_settings, s3_store):
        from main import app

        seed = tmp_path / "seed.db"
        _make_streaming_db(seed, albums=5, apple=3)
        await s3_store.put(STREAMING_DB_FILENAME, seed)

        resp = await self._download(app, admin_settings, s3_store)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/octet-stream"
        assert resp.content == seed.read_bytes()


def test_object_not_found_error_is_importable():
    """Guards the symbol the endpoints branch on to map absence -> accept / 404."""
    assert issubclass(ObjectNotFoundError, Exception)
