"""Size guards + backup rotation for POST /admin/upload-library-db (LML#1313).

The endpoint used to accept any SQLite file that parsed, publish it, and keep no
previous copy — so a truncated ``library.db`` was served, and the only way back
was re-running the producer. These tests pin the three things that changed:

* an **absolute** row floor (``LIBRARY_DB_MIN_ROWS``, 0 to opt out) rejecting at
  400 with both counts named;
* a **relative** guard against the currently-served copy, rejecting at 409 with
  the ``_check_count_regression`` record shape the streaming guard already uses;
* a single fixed ``library.db.previous`` backup key, written server-side via
  ``ObjectStore.copy`` before the new object lands.

A rejection must be inert: served file byte-identical, nothing in the store, no
scratch file left behind. That last property is why these assertions check the
tmp_path listing rather than only the response code.
"""

from __future__ import annotations

import logging
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
from routers.admin import LIBRARY_DB_FILENAME, LIBRARY_DB_PREVIOUS_FILENAME
from storage.object_store import S3ObjectStore
from tests.unit.conftest import override_deps

BUCKET = "lml-library-floor-test"
ENDPOINT = "https://s3.amazonaws.com"


def _make_library_db(path: Path, rows: int) -> Path:
    """Write a minimal but valid ``library.db`` carrying exactly ``rows`` rows."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE library ("
            "id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
            "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
            "genre TEXT, format TEXT)"
        )
        conn.executemany(
            "INSERT INTO library (id, title, artist, call_letters) VALUES (?, ?, ?, ?)",
            [(i, f"Aluminum Tunes {i}", "Stereolab", "S") for i in range(1, rows + 1)],
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def s3_store(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield S3ObjectStore(bucket=BUCKET, endpoint_url=ENDPOINT, region="us-east-1")


def _settings(tmp_path: Path, *, min_rows: int) -> Settings:
    # The serving directory exists in every deployed environment (the volume /
    # boot-fetch target), so create it here rather than letting its absence stand
    # in for "no served copy" -- that case is its own test.
    (tmp_path / "served").mkdir(parents=True, exist_ok=True)
    return Settings(
        admin_token="floor-token",
        library_db_path=tmp_path / "served" / "library.db",
        library_db_min_rows=min_rows,
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
    )


async def _upload(app, settings, store, db_file: Path, *, force: bool = False):
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
                    params={"force": "true"} if force else {},
                    headers={"Authorization": "Bearer floor-token"},
                    files={"file": ("library.db", f, "application/octet-stream")},
                )


def _seed_served_copy(settings: Settings, rows: int) -> Path:
    """Put a valid ``rows``-row catalog where the replica serves from."""
    served = settings.resolved_library_db_path
    served.parent.mkdir(parents=True, exist_ok=True)
    return _make_library_db(served, rows)


class TestAbsoluteRowFloor:
    def test_default_floor_is_anchored_to_the_real_catalog(self):
        """Not a round number pulled from the air: derived from the ~64,800-row shelf.

        Bounded on both sides — a floor at or above the live catalog would reject
        every honest upload, and one far below it would wave through the truncation
        this guard exists to catch.
        """
        floor = Settings().library_db_min_rows
        assert 50_000 <= floor < 64_800

    @pytest.mark.asyncio
    async def test_below_floor_is_rejected_with_both_counts(self, tmp_path, s3_store):
        from main import app

        settings = _settings(tmp_path, min_rows=500)
        upload = _make_library_db(tmp_path / "upload.db", 3)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 400
        detail = resp.json()["details"]["detail"]
        assert detail["row_count"] == 3
        assert detail["required_min_rows"] == 500

    @pytest.mark.asyncio
    async def test_below_floor_leaves_the_served_file_byte_identical(self, tmp_path, s3_store):
        from main import app

        settings = _settings(tmp_path, min_rows=500)
        served = _seed_served_copy(settings, 20)
        before = served.read_bytes()
        upload = _make_library_db(tmp_path / "upload.db", 3)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 400
        assert served.read_bytes() == before

    @pytest.mark.asyncio
    async def test_below_floor_writes_nothing_and_strands_no_scratch_file(self, tmp_path, s3_store):
        from main import app

        settings = _settings(tmp_path, min_rows=500)
        served = _seed_served_copy(settings, 20)
        upload = _make_library_db(tmp_path / "upload.db", 3)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 400
        assert await s3_store.exists(LIBRARY_DB_FILENAME) is False
        assert [p.name for p in served.parent.iterdir()] == ["library.db"]

    @pytest.mark.asyncio
    async def test_at_the_floor_is_accepted(self, tmp_path, s3_store):
        from main import app

        settings = _settings(tmp_path, min_rows=10)
        upload = _make_library_db(tmp_path / "upload.db", 10)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 200
        assert resp.json()["row_count"] == 10

    @pytest.mark.asyncio
    async def test_floor_of_zero_opts_out(self, tmp_path, s3_store):
        """0 is the documented escape hatch for an environment with a tiny catalog."""
        from main import app

        settings = _settings(tmp_path, min_rows=0)
        upload = _make_library_db(tmp_path / "upload.db", 1)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_force_overrides_the_floor_and_says_so_loudly(self, tmp_path, s3_store, caplog):
        from main import app

        settings = _settings(tmp_path, min_rows=500)
        upload = _make_library_db(tmp_path / "upload.db", 3)

        with caplog.at_level(logging.WARNING, logger="routers.admin"):
            resp = await _upload(app, settings, s3_store, upload, force=True)

        assert resp.status_code == 200
        assert any(
            rec.levelno >= logging.WARNING and "force=true" in rec.getMessage()
            for rec in caplog.records
        )


class TestRelativeRowGuard:
    @pytest.mark.asyncio
    async def test_drop_beyond_tolerance_is_rejected(self, tmp_path, s3_store):
        from main import app

        settings = _settings(tmp_path, min_rows=0)
        _seed_served_copy(settings, 1000)
        upload = _make_library_db(tmp_path / "upload.db", 400)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 409
        detail = resp.json()["details"]["detail"]
        assert detail["regressions"] == [
            {"metric": "library_rows", "old": 1000, "new": 400, "floor": 950.0}
        ]

    @pytest.mark.asyncio
    async def test_drop_beyond_tolerance_leaves_the_served_file_byte_identical(
        self, tmp_path, s3_store
    ):
        from main import app

        settings = _settings(tmp_path, min_rows=0)
        served = _seed_served_copy(settings, 1000)
        before = served.read_bytes()
        upload = _make_library_db(tmp_path / "upload.db", 400)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 409
        assert served.read_bytes() == before
        assert await s3_store.exists(LIBRARY_DB_FILENAME) is False
        assert [p.name for p in served.parent.iterdir()] == ["library.db"]

    @pytest.mark.asyncio
    async def test_drop_within_tolerance_is_accepted(self, tmp_path, s3_store):
        """Deaccessioning a few shelves is normal churn, not a truncation."""
        from main import app

        settings = _settings(tmp_path, min_rows=0)
        _seed_served_copy(settings, 1000)
        upload = _make_library_db(tmp_path / "upload.db", 980)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_served_copy_means_no_baseline(self, tmp_path, s3_store):
        """A replica with no local catalog yet must still be able to bootstrap one."""
        from main import app

        settings = _settings(tmp_path, min_rows=0)
        settings.resolved_library_db_path.parent.mkdir(parents=True, exist_ok=True)
        upload = _make_library_db(tmp_path / "upload.db", 2)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_unreadable_served_copy_means_no_baseline(self, tmp_path, s3_store):
        """Fails *open*, unlike the streaming guard — deliberately.

        An unreadable local catalog means this replica is already degraded, and
        this endpoint is its recovery path; failing closed would lock recovery
        behind ``force``. The absolute floor still applies.
        """
        from main import app

        settings = _settings(tmp_path, min_rows=0)
        served = settings.resolved_library_db_path
        served.parent.mkdir(parents=True, exist_ok=True)
        served.write_bytes(b"not a sqlite file at all")
        upload = _make_library_db(tmp_path / "upload.db", 2)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_force_overrides_the_regression_and_says_so_loudly(
        self, tmp_path, s3_store, caplog
    ):
        from main import app

        settings = _settings(tmp_path, min_rows=0)
        _seed_served_copy(settings, 1000)
        upload = _make_library_db(tmp_path / "upload.db", 400)

        with caplog.at_level(logging.WARNING, logger="routers.admin"):
            resp = await _upload(app, settings, s3_store, upload, force=True)

        assert resp.status_code == 200
        assert any(
            rec.levelno >= logging.WARNING and "force=true" in rec.getMessage()
            for rec in caplog.records
        )


class TestPreviousCopyRotation:
    @pytest.mark.asyncio
    async def test_successful_upload_preserves_the_outgoing_object(self, tmp_path, s3_store):
        from main import app

        settings = _settings(tmp_path, min_rows=0)
        await s3_store.put(LIBRARY_DB_FILENAME, b"yesterdays-catalog")
        upload = _make_library_db(tmp_path / "upload.db", 5)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 200
        assert await s3_store.get(LIBRARY_DB_PREVIOUS_FILENAME) == b"yesterdays-catalog"
        assert await s3_store.get(LIBRARY_DB_FILENAME) != b"yesterdays-catalog"

    @pytest.mark.asyncio
    async def test_overwriting_the_backup_key_is_the_rotation(self, tmp_path, s3_store):
        """One generation, bounded by construction — no delete, nothing to GC."""
        from main import app

        settings = _settings(tmp_path, min_rows=0)
        await s3_store.put(LIBRARY_DB_PREVIOUS_FILENAME, b"two-days-ago")
        await s3_store.put(LIBRARY_DB_FILENAME, b"yesterdays-catalog")
        upload = _make_library_db(tmp_path / "upload.db", 5)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 200
        assert await s3_store.get(LIBRARY_DB_PREVIOUS_FILENAME) == b"yesterdays-catalog"

    @pytest.mark.asyncio
    async def test_first_upload_has_nothing_to_back_up(self, tmp_path, s3_store):
        from main import app

        settings = _settings(tmp_path, min_rows=0)
        upload = _make_library_db(tmp_path / "upload.db", 5)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 200
        assert await s3_store.exists(LIBRARY_DB_PREVIOUS_FILENAME) is False

    @pytest.mark.asyncio
    async def test_rejected_upload_does_not_rotate_the_backup(self, tmp_path, s3_store):
        """The whole point: a bad upload must not consume the one generation we keep."""
        from main import app

        settings = _settings(tmp_path, min_rows=500)
        await s3_store.put(LIBRARY_DB_PREVIOUS_FILENAME, b"the-good-copy")
        await s3_store.put(LIBRARY_DB_FILENAME, b"yesterdays-catalog")
        upload = _make_library_db(tmp_path / "upload.db", 3)

        resp = await _upload(app, settings, s3_store, upload)

        assert resp.status_code == 400
        assert await s3_store.get(LIBRARY_DB_PREVIOUS_FILENAME) == b"the-good-copy"
        assert await s3_store.get(LIBRARY_DB_FILENAME) == b"yesterdays-catalog"

    @pytest.mark.asyncio
    async def test_backup_uses_the_store_copy_not_a_read_write_roundtrip(self, tmp_path):
        """The ~16MB blob must never transit this process (LML#1313 memory profile)."""
        from main import app

        settings = _settings(tmp_path, min_rows=0)
        store = AsyncMock()
        upload = _make_library_db(tmp_path / "upload.db", 5)

        resp = await _upload(app, settings, store, upload)

        assert resp.status_code == 200
        store.copy.assert_awaited_once_with(LIBRARY_DB_FILENAME, LIBRARY_DB_PREVIOUS_FILENAME)
        store.get.assert_not_awaited()
