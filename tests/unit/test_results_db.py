"""Unit tests for scripts/streaming_availability/results_db.py."""

import json

import aiosqlite
import pytest
import pytest_asyncio

from clients.streaming.matching import normalize_album_title, normalize_artist_name
from scripts.streaming_availability.dedup import DeduplicatedAlbum
from scripts.streaming_availability.results_db import ResultsDB


@pytest_asyncio.fixture
async def db():
    """In-memory results database."""
    results_db = ResultsDB(":memory:")
    await results_db.connect()
    yield results_db
    await results_db.close()


def _make_album(
    normalized_artist: str = "stereolab",
    normalized_title: str = "aluminum tunes",
    display_artist: str = "Stereolab",
    display_title: str = "Aluminum Tunes",
    library_ids: list[int] | None = None,
    formats: list[str] | None = None,
    genre: str | None = "Rock",
    label: str | None = "Duophonic",
    is_compilation: bool = False,
) -> DeduplicatedAlbum:
    return DeduplicatedAlbum(
        normalized_artist=normalized_artist,
        normalized_title=normalized_title,
        display_artist=display_artist,
        display_title=display_title,
        library_ids=library_ids if library_ids is not None else [1, 2],
        formats=formats if formats is not None else ["cd", "vinyl - LP"],
        genre=genre,
        label=label,
        is_compilation=is_compilation,
    )


class TestInsertAlbums:
    @pytest.mark.asyncio
    async def test_insert_single_album(self, db):
        album = _make_album()
        count = await db.insert_albums([album])
        assert count == 1

    @pytest.mark.asyncio
    async def test_insert_duplicate_ignored(self, db):
        album = _make_album()
        await db.insert_albums([album])
        count = await db.insert_albums([album])
        assert count == 0

    @pytest.mark.asyncio
    async def test_insert_multiple_albums(self, db):
        albums = [
            _make_album(),
            _make_album(
                normalized_artist="autechre",
                normalized_title="confield",
                display_artist="Autechre",
                display_title="Confield",
                library_ids=[3],
                formats=["cd"],
                genre="Electronic",
                label="Warp",
            ),
        ]
        count = await db.insert_albums(albums)
        assert count == 2

    @pytest.mark.asyncio
    async def test_library_ids_stored_as_json(self, db):
        album = _make_album(library_ids=[10, 20, 30])
        await db.insert_albums([album])
        rows = await db.get_pending("spotify", limit=10)
        assert json.loads(rows[0]["library_ids"]) == [10, 20, 30]

    @pytest.mark.asyncio
    async def test_compilation_stored(self, db):
        album = _make_album(
            normalized_artist="various artists",
            display_artist="Various Artists",
            is_compilation=True,
        )
        await db.insert_albums([album])
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["is_compilation"] == 1


class TestGetPending:
    @pytest.mark.asyncio
    async def test_returns_pending_rows(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        assert len(rows) == 1
        assert rows[0]["spotify_status"] == "pending"

    @pytest.mark.asyncio
    async def test_respects_limit(self, db):
        albums = [
            _make_album(),
            _make_album(
                normalized_artist="autechre",
                normalized_title="confield",
                display_artist="Autechre",
                display_title="Confield",
                library_ids=[3],
                formats=["cd"],
            ),
        ]
        await db.insert_albums(albums)
        rows = await db.get_pending("spotify", limit=1)
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_excludes_non_pending(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_result(album_id, "spotify", "found", url="https://open.spotify.com/album/x")
        rows = await db.get_pending("spotify", limit=10)
        assert len(rows) == 0


class TestGetSpotifyMissesPendingApple:
    @pytest.mark.asyncio
    async def test_returns_not_found_on_spotify_pending_on_apple(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_result(rows[0]["id"], "spotify", "not_found")
        rows = await db.get_spotify_misses_pending_apple(limit=10)
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_excludes_spotify_found(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_result(
            rows[0]["id"], "spotify", "found", url="https://open.spotify.com/album/x"
        )
        rows = await db.get_spotify_misses_pending_apple(limit=10)
        assert len(rows) == 0


class TestUpdateResult:
    @pytest.mark.asyncio
    async def test_update_spotify_found(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_result(
            album_id,
            "spotify",
            "found",
            url="https://open.spotify.com/album/abc123",
            spotify_id="abc123",
            confidence=95.0,
            matched_artist="Stereolab",
            matched_title="Aluminium Tunes",
        )
        stats = await db.get_stats()
        assert stats["spotify"]["found"] == 1

    @pytest.mark.asyncio
    async def test_update_apple_found(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_result(
            album_id,
            "apple",
            "found",
            url="https://music.apple.com/us/album/123",
            confidence=92.0,
            matched_artist="Stereolab",
            matched_title="Aluminum Tunes",
        )
        stats = await db.get_stats()
        assert stats["apple"]["found"] == 1


class TestGetStats:
    @pytest.mark.asyncio
    async def test_stats_all_pending(self, db):
        await db.insert_albums([_make_album()])
        stats = await db.get_stats()
        assert stats["spotify"]["pending"] == 1
        assert stats["apple"]["pending"] == 1
        assert stats["total"] == 1

    @pytest.mark.asyncio
    async def test_stats_empty_db(self, db):
        stats = await db.get_stats()
        assert stats["total"] == 0

    @pytest.mark.asyncio
    async def test_stats_includes_bandcamp(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_bandcamp_slug(album_id, "stereolab")
        await db.update_bandcamp_url(
            album_id, "https://stereolab.bandcamp.com/album/aluminum-tunes"
        )
        stats = await db.get_stats()
        assert stats["bandcamp"]["found"] == 1


class TestBandcampSlugMigration:
    @pytest.mark.asyncio
    async def test_bandcamp_slug_column_exists(self, db):
        """The bandcamp_slug column should be created by migration."""
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_slug"] is None

    @pytest.mark.asyncio
    async def test_set_bandcamp_slug(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_bandcamp_slug(album_id, "stereolab")
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_slug"] == "stereolab"


class TestGetArtistsWithoutBandcampSlug:
    @pytest.mark.asyncio
    async def test_returns_artists_without_slug(self, db):
        await db.insert_albums(
            [
                _make_album(),
                _make_album(
                    normalized_artist="autechre",
                    normalized_title="confield",
                    display_artist="Autechre",
                    display_title="Confield",
                    library_ids=[3],
                    formats=["cd"],
                ),
            ]
        )
        rows = await db.get_artists_without_bandcamp_slug(not_on_streaming_only=False)
        artists = [r["display_artist"] for r in rows]
        assert "Stereolab" in artists
        assert "Autechre" in artists

    @pytest.mark.asyncio
    async def test_excludes_artists_with_slug(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "stereolab")
        rows = await db.get_artists_without_bandcamp_slug(not_on_streaming_only=False)
        artists = [r["display_artist"] for r in rows]
        assert "Stereolab" not in artists

    @pytest.mark.asyncio
    async def test_excludes_compilations(self, db):
        await db.insert_albums(
            [
                _make_album(
                    normalized_artist="various artists",
                    display_artist="Various Artists",
                    is_compilation=True,
                ),
            ]
        )
        rows = await db.get_artists_without_bandcamp_slug(not_on_streaming_only=False)
        artists = [r["display_artist"] for r in rows]
        assert "Various Artists" not in artists

    @pytest.mark.asyncio
    async def test_not_on_streaming_only_filters(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_result(album_id, "spotify", "found", url="https://open.spotify.com/album/x")
        # With not_on_streaming_only=True, found-on-spotify albums should be excluded
        rows = await db.get_artists_without_bandcamp_slug(not_on_streaming_only=True)
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_not_on_streaming_includes_not_found(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_result(album_id, "spotify", "not_found")
        rows = await db.get_artists_without_bandcamp_slug(not_on_streaming_only=True)
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_respects_limit(self, db):
        await db.insert_albums(
            [
                _make_album(),
                _make_album(
                    normalized_artist="autechre",
                    normalized_title="confield",
                    display_artist="Autechre",
                    display_title="Confield",
                    library_ids=[3],
                    formats=["cd"],
                ),
            ]
        )
        rows = await db.get_artists_without_bandcamp_slug(not_on_streaming_only=False, limit=1)
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_returns_distinct_artists(self, db):
        await db.insert_albums(
            [
                _make_album(),
                _make_album(
                    normalized_artist="stereolab",
                    normalized_title="dots and loops",
                    display_artist="Stereolab",
                    display_title="Dots and Loops",
                    library_ids=[5],
                    formats=["cd"],
                ),
            ]
        )
        rows = await db.get_artists_without_bandcamp_slug(not_on_streaming_only=False)
        artists = [r["display_artist"] for r in rows]
        assert artists.count("Stereolab") == 1


class TestGetPendingBandcampLookup:
    @pytest.mark.asyncio
    async def test_returns_albums_with_slug_and_no_url(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "stereolab")
        pending = await db.get_pending_bandcamp_lookup()
        assert len(pending) == 1
        assert pending[0]["bandcamp_slug"] == "stereolab"

    @pytest.mark.asyncio
    async def test_excludes_empty_string_slug(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "")
        pending = await db.get_pending_bandcamp_lookup()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_excludes_albums_with_bandcamp_url(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_bandcamp_slug(album_id, "stereolab")
        await db.update_bandcamp_url(
            album_id, "https://stereolab.bandcamp.com/album/aluminum-tunes"
        )
        pending = await db.get_pending_bandcamp_lookup()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_excludes_compilations(self, db):
        await db.insert_albums(
            [
                _make_album(
                    normalized_artist="various artists",
                    display_artist="Various Artists",
                    is_compilation=True,
                ),
            ]
        )
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "someslug")
        pending = await db.get_pending_bandcamp_lookup()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_respects_limit(self, db):
        await db.insert_albums(
            [
                _make_album(),
                _make_album(
                    normalized_artist="autechre",
                    normalized_title="confield",
                    display_artist="Autechre",
                    display_title="Confield",
                    library_ids=[3],
                    formats=["cd"],
                ),
            ]
        )
        all_rows = await db.get_pending("spotify", limit=10)
        for r in all_rows:
            await db.update_bandcamp_slug(r["id"], "someslug")
        pending = await db.get_pending_bandcamp_lookup(limit=1)
        assert len(pending) == 1

    @pytest.mark.asyncio
    async def test_slug_filter_restricts_to_one_slug(self, db):
        await db.insert_albums(
            [
                _make_album(),
                _make_album(
                    normalized_artist="autechre",
                    normalized_title="confield",
                    display_artist="Autechre",
                    display_title="Confield",
                    library_ids=[3],
                    formats=["cd"],
                ),
            ]
        )
        all_rows = await db.get_pending("spotify", limit=10)
        for r in all_rows:
            await db.update_bandcamp_slug(r["id"], r["display_artist"].lower())

        pending = await db.get_pending_bandcamp_lookup(slug="autechre")
        assert len(pending) == 1
        assert pending[0]["bandcamp_slug"] == "autechre"


class TestBandcampStatusMarker:
    """Phase-2 resumability marker (#661): bandcamp_status / bandcamp_checked_at."""

    @pytest.mark.asyncio
    async def test_status_defaults_to_pending(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_status"] == "pending"

    @pytest.mark.asyncio
    async def test_checked_at_defaults_to_none(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_checked_at"] is None

    @pytest.mark.asyncio
    async def test_update_bandcamp_url_marks_found(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_bandcamp_slug(album_id, "stereolab")
        await db.update_bandcamp_url(
            album_id, "https://stereolab.bandcamp.com/album/aluminum-tunes"
        )
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_status"] == "found"
        assert rows[0]["bandcamp_checked_at"] is not None

    @pytest.mark.asyncio
    async def test_mark_not_found_sets_status_and_timestamp(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.mark_bandcamp_not_found(album_id)
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_status"] == "not_found"
        assert rows[0]["bandcamp_checked_at"] is not None
        assert rows[0]["bandcamp_url"] is None

    @pytest.mark.asyncio
    async def test_pending_excludes_attempted_not_found(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        album_id = rows[0]["id"]
        await db.update_bandcamp_slug(album_id, "stereolab")
        assert len(await db.get_pending_bandcamp_lookup()) == 1
        await db.mark_bandcamp_not_found(album_id)
        assert len(await db.get_pending_bandcamp_lookup()) == 0

    @pytest.mark.asyncio
    async def test_pending_includes_untried_slug(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "stereolab")
        pending = await db.get_pending_bandcamp_lookup()
        assert len(pending) == 1
        assert pending[0]["bandcamp_status"] == "pending"


class TestBandcampStatusMigrationOnExistingDb:
    """Exercise the ALTER-table path against a DB created before the column.

    A CREATE-TABLE-only definition would pass on a fresh DB but silently miss
    the column on a pre-existing file -- this test catches that regression.
    """

    OLD_SCHEMA = """
        CREATE TABLE albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            normalized_artist TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            display_artist TEXT NOT NULL,
            display_title TEXT NOT NULL,
            library_ids TEXT NOT NULL,
            formats TEXT NOT NULL,
            is_compilation INTEGER NOT NULL DEFAULT 0,
            spotify_status TEXT NOT NULL DEFAULT 'pending',
            apple_status TEXT NOT NULL DEFAULT 'pending',
            bandcamp_slug TEXT,
            bandcamp_url TEXT,
            UNIQUE(normalized_artist, normalized_title)
        )
    """

    async def _make_old_db(self, path: str, *, slug: str, url: str | None) -> None:
        conn = await aiosqlite.connect(path)
        try:
            await conn.executescript(self.OLD_SCHEMA)
            await conn.execute(
                "INSERT INTO albums "
                "(normalized_artist, normalized_title, display_artist, display_title, "
                " library_ids, formats, bandcamp_slug, bandcamp_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "stereolab",
                    "aluminum tunes",
                    "Stereolab",
                    "Aluminum Tunes",
                    "[1]",
                    '["cd"]',
                    slug,
                    url,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_migration_adds_status_and_gates_pending(self, tmp_path):
        path = str(tmp_path / "old.db")
        await self._make_old_db(path, slug="stereolab", url=None)

        db = ResultsDB(path)
        await db.connect()
        try:
            pending = await db.get_pending_bandcamp_lookup()
            assert len(pending) == 1
            assert pending[0]["bandcamp_status"] == "pending"
            await db.mark_bandcamp_not_found(pending[0]["id"])
            assert len(await db.get_pending_bandcamp_lookup()) == 0
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_migration_backfills_found_for_resolved_url(self, tmp_path):
        path = str(tmp_path / "old.db")
        await self._make_old_db(
            path,
            slug="stereolab",
            url="https://stereolab.bandcamp.com/album/aluminum-tunes",
        )

        db = ResultsDB(path)
        await db.connect()
        try:
            rows = await db.get_all_results()
            assert rows[0]["bandcamp_status"] == "found"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_backfill_runs_once_and_preserves_manual_reset(self, tmp_path):
        # The pending->found backfill must run only when the column is first
        # added, so a later connect never re-stamps a row a user deliberately
        # reset to 'pending' (URL kept) to force Phase-2 re-matching. Removing
        # the bandcamp_status_added guard would clobber that reset -- this test
        # is the regression fence.
        path = str(tmp_path / "reset.db")

        db = ResultsDB(path)
        await db.connect()
        try:
            await db.insert_albums([_make_album()])
            rows = await db.get_pending("spotify", limit=10)
            album_id = rows[0]["id"]
            await db.update_bandcamp_url(
                album_id, "https://stereolab.bandcamp.com/album/aluminum-tunes"
            )
            await db._db.execute(
                "UPDATE albums SET bandcamp_status = 'pending' WHERE id = ?", (album_id,)
            )
            await db._db.commit()
        finally:
            await db.close()

        db2 = ResultsDB(path)
        await db2.connect()
        try:
            rows = await db2.get_all_results()
            assert rows[0]["bandcamp_status"] == "pending"
        finally:
            await db2.close()


class TestYouTubeMusicFillOnlyWrite:
    """Fill-only youtube_music_url writer for the #1056 YTM coverage drain.

    The drain resolves verified music.youtube.com/browse/<id> links offline and
    writes them into the authoritative streaming_availability store (Option A of
    the #1056 / #1052 write-path fork). Writes are fill-only: a resolved URL is
    the top-priority link and must never be clobbered by a later, lower-value
    pass (Data Safety; the #669 status-marker lesson).
    """

    @pytest.mark.asyncio
    async def test_columns_exist_and_default(self, db):
        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["youtube_music_url"] is None
        assert rows[0]["youtube_music_status"] == "pending"
        assert rows[0]["youtube_music_checked_at"] is None

    @pytest.mark.asyncio
    async def test_write_sets_url_status_and_timestamp(self, db):
        await db.insert_albums([_make_album()])
        album_id = (await db.get_pending("spotify", limit=10))[0]["id"]
        url = "https://music.youtube.com/browse/MPREb_stereolab"
        wrote = await db.update_youtube_music_url(album_id, url)
        assert wrote is True
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["youtube_music_url"] == url
        assert rows[0]["youtube_music_status"] == "found"
        assert rows[0]["youtube_music_checked_at"] is not None

    @pytest.mark.asyncio
    async def test_fill_only_does_not_overwrite_existing_url(self, db):
        await db.insert_albums([_make_album()])
        album_id = (await db.get_pending("spotify", limit=10))[0]["id"]
        first = "https://music.youtube.com/browse/MPREb_first"
        await db.update_youtube_music_url(album_id, first)
        wrote = await db.update_youtube_music_url(
            album_id, "https://music.youtube.com/browse/MPREb_second"
        )
        assert wrote is False  # already filled -> no-op
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["youtube_music_url"] == first  # original preserved

    @pytest.mark.asyncio
    async def test_write_to_missing_album_is_noop(self, db):
        wrote = await db.update_youtube_music_url(9999, "https://music.youtube.com/browse/MPREb_x")
        assert wrote is False

    @pytest.mark.asyncio
    async def test_get_stats_includes_youtube_music(self, db):
        await db.insert_albums([_make_album()])
        album_id = (await db.get_pending("spotify", limit=10))[0]["id"]
        await db.update_youtube_music_url(album_id, "https://music.youtube.com/browse/MPREb_x")
        stats = await db.get_stats()
        assert stats["youtube_music"]["found"] == 1


class TestGetAlbumIdByNames:
    """execute_write maps a resolved candidate to its albums row by normalized
    (artist, title) -- the exact key the dedup pipeline wrote (dedup.py) -- so
    there is no normalization drift between producer and store."""

    @pytest.mark.asyncio
    async def test_finds_row_via_normalized_key(self, db):
        album = _make_album(
            normalized_artist=normalize_artist_name("Stereolab"),
            normalized_title=normalize_album_title("Aluminum Tunes"),
            display_artist="Stereolab",
            display_title="Aluminum Tunes",
        )
        await db.insert_albums([album])
        # Raw display-cased names normalize to the stored key.
        album_id = await db.get_album_id_by_names("Stereolab", "Aluminum Tunes")
        assert album_id is not None
        rows = await db.get_pending("spotify", limit=10)
        assert album_id == rows[0]["id"]

    @pytest.mark.asyncio
    async def test_returns_none_when_absent(self, db):
        assert await db.get_album_id_by_names("Nonexistent Artist", "No Such Album") is None


class TestYouTubeMusicMigrationOnExistingDb:
    """Exercise the ALTER path against a DB created before the youtube_music_*
    columns -- a CREATE-only definition would pass on a fresh DB but silently
    miss the columns on a pre-existing file (the same regression fence as
    TestBandcampStatusMigrationOnExistingDb)."""

    OLD_SCHEMA = """
        CREATE TABLE albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            normalized_artist TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            display_artist TEXT NOT NULL,
            display_title TEXT NOT NULL,
            library_ids TEXT NOT NULL,
            formats TEXT NOT NULL,
            is_compilation INTEGER NOT NULL DEFAULT 0,
            spotify_status TEXT NOT NULL DEFAULT 'pending',
            apple_status TEXT NOT NULL DEFAULT 'pending',
            youtube_music_url TEXT,
            UNIQUE(normalized_artist, normalized_title)
        )
    """

    async def _make_old_db(self, path: str, *, url: str | None) -> None:
        conn = await aiosqlite.connect(path)
        try:
            await conn.executescript(self.OLD_SCHEMA)
            await conn.execute(
                "INSERT INTO albums "
                "(normalized_artist, normalized_title, display_artist, display_title, "
                " library_ids, formats, youtube_music_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "stereolab",
                    "aluminum tunes",
                    "Stereolab",
                    "Aluminum Tunes",
                    "[1]",
                    '["cd"]',
                    url,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_migration_adds_status_and_checked_at(self, tmp_path):
        path = str(tmp_path / "old.db")
        await self._make_old_db(path, url=None)

        db = ResultsDB(path)
        await db.connect()
        try:
            rows = await db.get_all_results()
            assert rows[0]["youtube_music_status"] == "pending"
            assert rows[0]["youtube_music_checked_at"] is None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_migration_backfills_found_for_resolved_url(self, tmp_path):
        # A row that already carries a url (from the earlier pipeline era) must be
        # marked 'found', not left 'pending', so attempted-vs-resolved reporting is
        # trustworthy and a fill-only pass skips it (#661 pattern).
        path = str(tmp_path / "old.db")
        await self._make_old_db(path, url="https://music.youtube.com/browse/MPREb_pre")

        db = ResultsDB(path)
        await db.connect()
        try:
            rows = await db.get_all_results()
            assert rows[0]["youtube_music_status"] == "found"
            # Fill-only must not clobber the pre-existing url.
            wrote = await db.update_youtube_music_url(
                rows[0]["id"], "https://music.youtube.com/browse/MPREb_new"
            )
            assert wrote is False
            rows = await db.get_all_results()
            assert rows[0]["youtube_music_url"] == "https://music.youtube.com/browse/MPREb_pre"
        finally:
            await db.close()
