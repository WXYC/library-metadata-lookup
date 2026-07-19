"""Bucket-mode tests for POST /admin/upload-library-db (WXYC#837, volume-eviction PR 3).

The upload endpoint is mode-agnostic: it writes the validated file to the active
:class:`~storage.object_store.ObjectStore` (the durable canonical copy) and then
performs today's local hot-swap (``close_library_db()`` then ``os.replace``) so
the replica that served the request also picks up the new file. These tests pin
the bucket-mode leg against a moto-backed :class:`S3ObjectStore`; the existing
``test_admin_router.py`` suite proves the identical contract against the default
``LocalDirStore`` (local mode unchanged).
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
from routers.admin import LIBRARY_DB_FILENAME
from storage.object_store import S3ObjectStore
from tests.unit.conftest import override_deps
from tests.unit.test_admin_router import _make_valid_sqlite_db

BUCKET = "lml-library-upload-test"
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
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield S3ObjectStore(bucket=BUCKET, endpoint_url=ENDPOINT, region="us-east-1")


async def _upload(app, settings, store, db_file):
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
                    "/admin/upload-library-db",
                    headers={"Authorization": "Bearer bucket-token"},
                    files={"file": ("library.db", f, "application/octet-stream")},
                )


class TestUploadLibraryBucket:
    @pytest.mark.asyncio
    async def test_upload_puts_to_bucket(self, tmp_path, admin_settings, s3_store):
        """A valid upload lands in the bucket under the library.db key."""
        from main import app

        db_file = tmp_path / "upload.db"
        _make_valid_sqlite_db(db_file)

        resp = await _upload(app, admin_settings, s3_store, db_file)

        assert resp.status_code == 200
        assert resp.json()["row_count"] == 1
        assert await s3_store.exists(LIBRARY_DB_FILENAME) is True
        data = await s3_store.get(LIBRARY_DB_FILENAME)
        check = tmp_path / "from_bucket.db"
        check.write_bytes(data)
        conn = sqlite3.connect(str(check))
        try:
            assert conn.execute("SELECT count(*) FROM library").fetchone()[0] == 1
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_upload_hotswaps_local_file(self, tmp_path, admin_settings, s3_store):
        """The serving replica's on-disk library.db is replaced with the upload."""
        from main import app

        # Pre-existing stale local file to be replaced by the hot-swap.
        db_path: Path = admin_settings.resolved_library_db_path
        db_path.write_bytes(b"stale")

        db_file = tmp_path / "upload.db"
        _make_valid_sqlite_db(db_file)

        resp = await _upload(app, admin_settings, s3_store, db_file)

        assert resp.status_code == 200
        # Local file now a valid SQLite library DB, not the stale bytes.
        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute("SELECT count(*) FROM library").fetchone()[0] == 1
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_bucket_put_failure_does_not_hotswap(self, tmp_path, admin_settings):
        """If the durable put fails, the local file is left intact (put-first ordering)."""
        from main import app

        db_path: Path = admin_settings.resolved_library_db_path
        db_path.write_bytes(b"stale-but-canonical")

        db_file = tmp_path / "upload.db"
        _make_valid_sqlite_db(db_file)

        failing_store = AsyncMock()
        failing_store.put = AsyncMock(side_effect=RuntimeError("bucket down"))

        resp = await _upload(app, admin_settings, failing_store, db_file)

        assert resp.status_code == 500
        # Local file untouched — no half-applied swap after a failed durable write.
        assert db_path.read_bytes() == b"stale-but-canonical"
