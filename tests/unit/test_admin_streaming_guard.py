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
    track_results_usable: int | None = None,
    with_url_columns: bool = True,
) -> None:
    """Build a streaming_availability.db with controllable coverage metrics.

    Args:
        albums: number of rows in the `albums` table.
        apple/spotify/deezer: how many of those rows carry a non-null URL.
        track_results: row count for the `track_results` table; None omits the table.
        track_results_usable: how many of those rows are "usable" -- i.e. resolved
            (`resolution_status='local_match'`) AND carry a non-null URL, matching the
            predicate export_streaming_links.py uses. The remaining
            ``track_results - track_results_usable`` rows are unresolved
            (`resolution_status='pending'`, NULL URLs), like the rows the pipeline
            inserts up front before resolution. Defaults to ``track_results`` (all
            usable) so callers that only care about row count stay simple.
        with_url_columns: when False, the `albums` table has no URL columns at all
            (mirrors the minimal fixture in test_admin_streaming_db.py).

    Requires albums >= max(apple, spotify, deezer) and
    0 <= track_results_usable <= track_results.
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
        usable = track_results if track_results_usable is None else track_results_usable
        assert 0 <= usable <= track_results, "track_results_usable must be in [0, track_results]"
        conn.execute(
            "CREATE TABLE track_results ("
            "id INTEGER PRIMARY KEY, album_id INTEGER, "
            "spotify_url TEXT, deezer_url TEXT, resolution_status TEXT)"
        )
        for i in range(track_results):
            if i < usable:
                conn.execute(
                    "INSERT INTO track_results "
                    "(id, album_id, spotify_url, resolution_status) VALUES (?, ?, ?, ?)",
                    (i + 1, 1, f"https://open.spotify.com/track/{i}", "local_match"),
                )
            else:
                conn.execute(
                    "INSERT INTO track_results (id, album_id, resolution_status) VALUES (?, ?, ?)",
                    (i + 1, 1, "pending"),
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

    def test_track_results_counts_only_usable_rows(self, tmp_path):
        """track_results coverage counts resolved+URL-bearing rows, not raw COUNT(*).

        The pipeline inserts a track_results row per track up front
        (resolution_status='pending', NULL URLs) and fills URLs in later; the
        downstream export only consumes resolved rows with a non-null URL. A raw
        COUNT(*) would let an upload that nulls every track URL pass the guard, so
        the metric must mirror the export predicate.
        """
        from routers.admin import _streaming_coverage

        db = tmp_path / "s.db"
        _make_streaming_db(db, albums=10, track_results=100, track_results_usable=30)
        assert _streaming_coverage(db)["track_results"] == 30

    def test_track_results_with_legacy_schema_falls_back_to_rowcount(self, tmp_path):
        """A track_results table lacking the URL/status columns reads as COUNT(*).

        Keeps the column-defensive contract: an unexpected schema still produces a
        non-zero count so a disappearing table is caught as an N -> 0 regression.
        """
        from routers.admin import _streaming_coverage

        db = tmp_path / "s.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE albums (id INTEGER PRIMARY KEY, library_ids TEXT)")
        conn.execute("CREATE TABLE track_results (id INTEGER PRIMARY KEY, album_id INTEGER)")
        for i in range(7):
            conn.execute("INSERT INTO track_results (id, album_id) VALUES (?, ?)", (i + 1, 1))
        conn.commit()
        conn.close()
        assert _streaming_coverage(db)["track_results"] == 7


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

    @pytest.mark.asyncio
    async def test_unreadable_existing_db_rejected_not_fail_open(self, tmp_path, admin_settings):
        """A present-but-corrupt on-disk DB must fail closed, not wave the upload through.

        If the existing file can't be read, the guard has no baseline to compare
        against. Returning all-zero coverage would treat it as a first upload and
        accept any thin replacement -- exactly the 288 -> 0 incident the guard
        exists to prevent, fired precisely when the disk is flaky. Expect 409.
        """
        from main import app

        vol = self._volume_path(admin_settings)
        # exists() is True but it is not a valid SQLite file -> the coverage read raises.
        vol.write_bytes(b"this is not a sqlite database")

        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=300, apple=0, spotify=200, deezer=150)
        before = vol.read_bytes()

        resp = await self._upload(app, admin_settings, upload)
        assert resp.status_code == 409
        # The corrupt on-disk file is left untouched.
        assert vol.read_bytes() == before

    @pytest.mark.asyncio
    async def test_force_overrides_unreadable_existing_db(self, tmp_path, admin_settings):
        """force=true still lets an upload replace an unreadable on-disk DB."""
        from main import app

        vol = self._volume_path(admin_settings)
        vol.write_bytes(b"this is not a sqlite database")

        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=300, apple=288, spotify=200, deezer=150)

        resp = await self._upload(app, admin_settings, upload, force=True)
        assert resp.status_code == 200
        assert _read_apple_count(vol) == 288

    @pytest.mark.asyncio
    async def test_unreadable_existing_db_409_detail_has_no_regressions(
        self, tmp_path, admin_settings
    ):
        """The unreadable-baseline 409 carries the `error` discriminator but no `regressions`.

        Guards the contract a consumer branches on: a 409 always has `detail['error']`,
        and `detail['regressions']` is present only for the coverage-regression variant.
        """
        from main import app

        vol = self._volume_path(admin_settings)
        vol.write_bytes(b"this is not a sqlite database")
        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=300, apple=288, spotify=200, deezer=150)

        resp = await self._upload(app, admin_settings, upload)
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "error" in detail
        assert "regressions" not in detail

    @pytest.mark.asyncio
    async def test_unreadable_uploaded_tmp_after_validation_is_500(
        self, tmp_path, admin_settings, monkeypatch
    ):
        """A read fault on the just-validated upload is a server fault (500), not a 400.

        The upload passes the albums-count probe, then the coverage read of the tmp
        file faults -- a server-side problem with a file we just wrote, so a
        retry-on-5xx caller should retry. The tmp file must still be cleaned up.
        """
        import routers.admin as admin_mod
        from main import app

        # A healthy prior file on the volume, so old_cov reads fine and we reach
        # the tmp-file coverage read.
        vol = self._volume_path(admin_settings)
        _make_streaming_db(vol, albums=100, apple=50, spotify=80, deezer=60)

        upload = tmp_path / "u.db"
        _make_streaming_db(upload, albums=100, apple=50, spotify=80, deezer=60)

        real_coverage = admin_mod._streaming_coverage

        def fake_coverage(db_path):
            # Fault only on the freshly-written tmp file; delegate for the on-disk one.
            if str(db_path).endswith(".tmp"):
                raise admin_mod.StreamingCoverageUnreadableError(str(db_path))
            return real_coverage(db_path)

        monkeypatch.setattr(admin_mod, "_streaming_coverage", fake_coverage)

        resp = await self._upload(app, admin_settings, upload)
        assert resp.status_code == 500
        # The on-disk file is untouched and the tmp scratch file is cleaned up.
        assert _read_apple_count(vol) == 50
        assert not (vol.parent / (vol.name + ".tmp")).exists()


def _read_apple_count(path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT COUNT(apple_url) FROM albums").fetchone()[0]
    finally:
        conn.close()
