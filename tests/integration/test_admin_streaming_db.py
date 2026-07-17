"""Integration tests for /admin/upload-streaming-db + /admin/download-streaming-db.

Verifies the store round-trip: upload bytes, fetch them back, assert byte-equal.
This is the contract the discogs-etl daily sync depends on once it stops going
through the GitHub Release transport. Run against both store backends
(WXYC#836): ``LocalDirStore`` (the default, rooted at the library.db parent) and
a moto-backed ``S3ObjectStore`` (Railway Bucket), proving the contract is
identical in either mode.
"""

import sqlite3
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
from storage.object_store import S3ObjectStore
from tests.unit.conftest import override_deps


def _make_streaming_db(path, album_count: int = 3) -> bytes:
    """Create a minimal valid streaming_availability.db with an `albums` table.

    Returns the file's bytes so the caller can compare against the downloaded
    payload. The file is closed before bytes are read so SQLite has flushed.
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE albums ("
        "id INTEGER PRIMARY KEY, artist TEXT, title TEXT, on_streaming INTEGER)"
    )
    for i in range(album_count):
        conn.execute(
            "INSERT INTO albums (id, artist, title, on_streaming) VALUES (?, ?, ?, ?)",
            (i + 1, f"Artist {i}", f"Album {i}", 1),
        )
    conn.commit()
    conn.close()
    return path.read_bytes()


@pytest.fixture
def admin_settings(tmp_path):
    """Settings with a writable volume directory and a configured admin token."""
    return Settings(
        admin_token="round-trip-token",
        library_db_path=tmp_path / "library.db",
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
    )


class TestUploadDownloadRoundTrip:
    @pytest.mark.asyncio
    async def test_upload_then_download_byte_equal(self, tmp_path, admin_settings):
        """POST upload, GET download -> downloaded bytes equal uploaded bytes."""
        from main import app

        source_path = tmp_path / "source_streaming.db"
        original_bytes = _make_streaming_db(source_path, album_count=5)

        with override_deps(
            app,
            {
                get_library_db: AsyncMock(),
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: admin_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with open(source_path, "rb") as f:
                    upload_resp = await client.post(
                        "/admin/upload-streaming-db",
                        headers={"Authorization": "Bearer round-trip-token"},
                        files={
                            "file": (
                                "streaming_availability.db",
                                f,
                                "application/octet-stream",
                            )
                        },
                    )
                assert upload_resp.status_code == 200
                assert upload_resp.json()["status"] == "ok"
                assert upload_resp.json()["row_count"] == 5

                download_resp = await client.get(
                    "/admin/download-streaming-db",
                    headers={"Authorization": "Bearer round-trip-token"},
                )

        assert download_resp.status_code == 200
        assert download_resp.content == original_bytes
        assert download_resp.headers["content-type"] == "application/octet-stream"


class TestUploadDownloadRoundTripBucket:
    @pytest.mark.asyncio
    async def test_upload_then_download_byte_equal_in_bucket(
        self, tmp_path, admin_settings, monkeypatch
    ):
        """The same round-trip against a moto-backed S3 store (Railway Bucket).

        The store is overridden onto an ``S3ObjectStore`` so the endpoints write to
        and read from the (in-process) bucket; ``mock_aws`` wraps the whole exchange.
        """
        from main import app

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

        source_path = tmp_path / "source_streaming.db"
        original_bytes = _make_streaming_db(source_path, album_count=5)

        with mock_aws():
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="lml-rt-bucket")
            store = S3ObjectStore(
                bucket="lml-rt-bucket",
                endpoint_url="https://s3.amazonaws.com",
                region="us-east-1",
            )
            with override_deps(
                app,
                {
                    get_library_db: AsyncMock(),
                    get_discogs_service: None,
                    get_posthog_client: None,
                    get_settings: admin_settings,
                    get_object_store: store,
                },
            ):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as client:
                    with open(source_path, "rb") as f:
                        upload_resp = await client.post(
                            "/admin/upload-streaming-db",
                            headers={"Authorization": "Bearer round-trip-token"},
                            files={
                                "file": (
                                    "streaming_availability.db",
                                    f,
                                    "application/octet-stream",
                                )
                            },
                        )
                    assert upload_resp.status_code == 200
                    assert upload_resp.json()["row_count"] == 5

                    download_resp = await client.get(
                        "/admin/download-streaming-db",
                        headers={"Authorization": "Bearer round-trip-token"},
                    )

        assert download_resp.status_code == 200
        assert download_resp.content == original_bytes
        assert download_resp.headers["content-type"] == "application/octet-stream"
