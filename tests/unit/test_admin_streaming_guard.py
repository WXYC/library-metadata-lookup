"""Unit tests for the streaming_availability.db coverage-regression guard (LML#672).

`POST /admin/upload-streaming-db` is a full-file replace. Before #672 it validated
only "SQLite + albums table + rowcount", so a thin copy (e.g. one with the Apple
column stripped) could silently overwrite a rich one — exactly the 288 Apple URLs
-> 0 incident. The guard compares the upload against the file currently on disk
and rejects a copy where any guarded metric regresses, unless `force=true`.
"""

import sqlite3
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import Settings, get_settings
from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
from tests.unit.conftest import override_deps


def _make_streaming_db(
    path,
    *,
    albums: int = 0,
    apple: int = 0,
    spotify: int = 0,
    deezer: int = 0,
    track_results: int | None = None,
    with_url_columns: bool = True,
) -> None:
    """Build a streaming_availability.db with controllable coverage metrics.

    Args:
        albums: number of rows in the `albums` table.
        apple/spotify/deezer: how many of those rows carry a non-null URL.
        track_results: row count for the `track_results` table; None omits the table.
        with_url_columns: when False, the `albums` table has no URL columns at all
            (mirrors the minimal fixture in test_admin_streaming_db.py).

    Requires albums >= max(apple, spotify, deezer).
    """
    assert albums >= max(apple, spotify, deezer), "albums must be >= each URL count"
    conn = sqlite3.connect(str(path))
    if with_url_columns:
        conn.execute(
            "CREATE TABLE albums ("
            "id INTEGER PRIMARY KEY, library_ids TEXT, "
            "apple_url TEXT, spotify_url TEXT, deezer_url TEXT)"
        )
        for i in range(albums):
            conn.execute(
                "INSERT INTO albums (id, library_ids, apple_url, spotify_url, deezer_url) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    i + 1,
                    f"[{i + 1}]",
                    f"https://music.apple.com/album/{i}" if i < apple else None,
                    f"https://open.spotify.com/album/{i}" if i < spotify else None,
                    f"https://www.deezer.com/album/{i}" if i < deezer else None,
                ),
            )
    else:
        conn.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, library_ids TEXT)")
        for i in range(albums):
            conn.execute(
                "INSERT INTO albums (id, library_ids) VALUES (?, ?)", (i + 1, f"[{i + 1}]")
            )
    if track_results is not None:
        conn.execute(
            "CREATE TABLE track_results ("
            "id INTEGER PRIMARY KEY, album_id INTEGER, "
            "spotify_url TEXT, deezer_url TEXT, resolution_status TEXT)"
        )
        for i in range(track_results):
            conn.execute(
                "INSERT INTO track_results (id, album_id, resolution_status) VALUES (?, ?, ?)",
                (i + 1, 1, "local_match"),
            )
    conn.commit()
    conn.close()


@pytest.fixture
def admin_settings(tmp_path):
    return Settings(
        admin_token="guard-token",
        library_db_path=tmp_path / "library.db",
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestStreamingCoverage:
    def test_missing_file_all_zero(self, tmp_path):
        from routers.admin import _streaming_coverage

        cov = _streaming_coverage(tmp_path / "nope.db")
        assert cov == {
            "apple_url": 0,
            "spotify_url": 0,
            "deezer_url": 0,
            "albums": 0,
            "track_results": 0,
        }

    def test_counts_metrics(self, tmp_path):
        from routers.admin import _streaming_coverage

        db = tmp_path / "s.db"
        _make_streaming_db(db, albums=10, apple=4, spotify=7, deezer=5, track_results=12)
        cov = _streaming_coverage(db)
        assert cov == {
            "apple_url": 4,
            "spotify_url": 7,
            "deezer_url": 5,
            "albums": 10,
            "track_results": 12,
        }

    def test_missing_track_results_table_reads_zero(self, tmp_path):
        from routers.admin import _streaming_coverage

        db = tmp_path / "s.db"
        _make_streaming_db(db, albums=3, apple=1, track_results=None)
        cov = _streaming_coverage(db)
        assert cov["track_results"] == 0
        assert cov["albums"] == 3
        assert cov["apple_url"] == 1

    def test_missing_url_columns_read_zero(self, tmp_path):
        """A minimal albums table without URL columns must not raise (it reads 0)."""
        from routers.admin import _streaming_coverage

        db = tmp_path / "s.db"
        _make_streaming_db(db, albums=5, with_url_columns=False)
        cov = _streaming_coverage(db)
        assert cov == {
            "apple_url": 0,
            "spotify_url": 0,
            "deezer_url": 0,
            "albums": 5,
            "track_results": 0,
        }


class TestCheckStreamingRegression:
    def _cov(self, **kw):
        base = {"apple_url": 0, "spotify_url": 0, "deezer_url": 0, "albums": 0, "track_results": 0}
        base.update(kw)
        return base

    def test_growth_no_regression(self):
        from routers.admin import _check_streaming_regression

        old = self._cov(
            apple_url=100, spotify_url=200, deezer_url=150, albums=300, track_results=50
        )
        new = self._cov(
            apple_url=110, spotify_url=210, deezer_url=160, albums=320, track_results=60
        )
        assert _check_streaming_regression(old, new, tolerance=0.05) == []

    def test_first_upload_old_all_zero(self):
        from routers.admin import _check_streaming_regression

        old = self._cov()  # nothing on disk
        new = self._cov(apple_url=5, spotify_url=9, deezer_url=7, albums=10, track_results=3)
        assert _check_streaming_regression(old, new, tolerance=0.05) == []

    def test_nonzero_to_zero_flagged(self):
        from routers.admin import _check_streaming_regression

        old = self._cov(apple_url=288, spotify_url=200, deezer_url=150, albums=300)
        new = self._cov(apple_url=0, spotify_url=200, deezer_url=150, albums=300)
        regs = _check_streaming_regression(old, new, tolerance=0.05)
        assert [r["metric"] for r in regs] == ["apple_url"]
        assert regs[0]["old"] == 288
        assert regs[0]["new"] == 0

    @pytest.mark.parametrize(
        "tolerance,new_apple,expect_regression",
        [
            (0.05, 96, False),  # -4% within 5% band
            (0.05, 94, True),  # -6% breaches 5% band
            (0.10, 94, False),  # -6% within 10% band
            (0.10, 89, True),  # -11% breaches 10% band
        ],
    )
    def test_tolerance_boundary(self, tolerance, new_apple, expect_regression):
        from routers.admin import _check_streaming_regression

        old = self._cov(apple_url=100, albums=100)
        new = self._cov(apple_url=new_apple, albums=100)
        regs = _check_streaming_regression(old, new, tolerance=tolerance)
        assert bool(regs) is expect_regression

    def test_multiple_metrics_flagged(self):
        from routers.admin import _check_streaming_regression

        old = self._cov(
            apple_url=100, spotify_url=100, deezer_url=100, albums=100, track_results=100
        )
        new = self._cov(apple_url=50, spotify_url=100, deezer_url=50, albums=100, track_results=100)
        regs = _check_streaming_regression(old, new, tolerance=0.05)
        assert {r["metric"] for r in regs} == {"apple_url", "deezer_url"}


# ---------------------------------------------------------------------------
# Endpoint integration
# ---------------------------------------------------------------------------


class TestUploadStreamingGuard:
    async def _upload(self, app, settings, db_file, *, force=False):
        url = "/admin/upload-streaming-db" + ("?force=true" if force else "")
        with override_deps(
            app,
            {
                get_library_db: AsyncMock(),
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with open(db_file, "rb") as f:
                    return await client.post(
                        url,
                        headers={"Authorization": "Bearer guard-token"},
                        files={
                            "file": ("streaming_availability.db", f, "application/octet-stream")
                        },
                    )

    def _volume_path(self, settings):
        return settings.resolved_library_db_path.parent / "streaming_availability.db"

    @pytest.mark.asyncio
    async def test_first_upload_allowed(self, tmp_path, admin_settings):
        """No prior file on the volume -> the upload is accepted."""
        from main import app

        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=10, apple=5, spotify=8, deezer=6, track_results=20)

        resp = await self._upload(app, admin_settings, upload)
        assert resp.status_code == 200
        assert resp.json()["row_count"] == 10
        assert self._volume_path(admin_settings).exists()

    @pytest.mark.asyncio
    async def test_growth_allowed(self, tmp_path, admin_settings):
        from main import app

        _make_streaming_db(
            self._volume_path(admin_settings), albums=100, apple=50, spotify=80, deezer=60
        )
        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=110, apple=60, spotify=90, deezer=70)

        resp = await self._upload(app, admin_settings, upload)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_within_tolerance_allowed(self, tmp_path, admin_settings):
        """A <5% shrink (legitimate churn) passes."""
        from main import app

        _make_streaming_db(self._volume_path(admin_settings), albums=100, apple=100)
        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=100, apple=97)  # -3%

        resp = await self._upload(app, admin_settings, upload)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_apple_nonzero_to_zero_rejected(self, tmp_path, admin_settings):
        """The 288 -> 0 incident: a copy with no Apple URLs is rejected."""
        from main import app

        vol = self._volume_path(admin_settings)
        _make_streaming_db(vol, albums=300, apple=288, spotify=200, deezer=150)
        before = vol.read_bytes()

        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=300, apple=0, spotify=200, deezer=150)

        resp = await self._upload(app, admin_settings, upload)
        assert resp.status_code == 409
        regs = resp.json()["detail"]["regressions"]
        assert any(r["metric"] == "apple_url" for r in regs)
        # On-disk file is untouched.
        assert vol.read_bytes() == before

    @pytest.mark.asyncio
    async def test_below_tolerance_rejected(self, tmp_path, admin_settings):
        from main import app

        _make_streaming_db(self._volume_path(admin_settings), albums=100, apple=100)
        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=100, apple=90)  # -10%

        resp = await self._upload(app, admin_settings, upload)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_missing_track_results_rejected(self, tmp_path, admin_settings):
        """Prior had track_results; upload omits the table -> N->0 regression."""
        from main import app

        _make_streaming_db(
            self._volume_path(admin_settings), albums=50, apple=10, track_results=100
        )
        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=50, apple=10, track_results=None)

        resp = await self._upload(app, admin_settings, upload)
        assert resp.status_code == 409
        regs = resp.json()["detail"]["regressions"]
        assert any(r["metric"] == "track_results" for r in regs)

    @pytest.mark.asyncio
    async def test_force_overrides_regression(self, tmp_path, admin_settings, caplog):
        """force=true accepts a regressing upload and logs loudly."""
        import logging

        from main import app

        vol = self._volume_path(admin_settings)
        _make_streaming_db(vol, albums=300, apple=288, spotify=200, deezer=150)
        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=300, apple=0, spotify=200, deezer=150)

        with caplog.at_level(logging.WARNING, logger="routers.admin"):
            resp = await self._upload(app, admin_settings, upload, force=True)

        assert resp.status_code == 200
        # The on-disk file is now the (thinner) uploaded copy.
        assert _read_apple_count(vol) == 0
        assert any("regress" in r.message.lower() for r in caplog.records)


def _read_apple_count(path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(apple_url) FROM albums").fetchone()[0]
    finally:
        conn.close()
