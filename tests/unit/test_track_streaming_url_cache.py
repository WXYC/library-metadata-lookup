"""Unit tests for ``entity.track_streaming_url_cache`` (get / set helpers).

The track-scoped streaming-URL cache (LML#893, lever L1) is a NEW LML-owned
table ``lml_cache.track_streaming_url_cache`` keyed on
``(service, artist_normalized, album_normalized, song_normalized)`` — a real
song axis, unlike the album-keyed ``lml_cache.album_streaming_url_cache``. It
caches the per-track Apple Music deep-link the ~4.8s ``find_track_url`` probe
resolves on the ``/lookup`` happy path so a repeat lookup of the same
``(artist, album, played-track)`` skips the live probe.

The cache module owns normalization via ``wxyc_etl.text.to_match_form`` so
callers pass raw request strings; the ``service`` key distinguishes the row.
A ``None`` album normalizes to the empty string so album-absent lookups have a
stable key. PG interaction is mocked with ``AsyncMock(spec=PgSource)``;
``tests/integration/test_track_streaming_url_cache_pg.py`` covers the real SQL
behavior end-to-end (including the different-song-same-album MISS).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from wxyc_etl.text import to_match_form

from entity.sources import PgSource
from entity.track_streaming_url_cache import (
    APPLE_MUSIC_TRACK_SERVICE,
    get_cached_track_streaming_url,
    set_cached_track_streaming_url,
)

_SAMPLE_URL = "https://music.apple.com/us/song/la-paradoja/1234567890"


@pytest.mark.asyncio
class TestGetCachedTrackStreamingURL:
    async def test_returns_none_when_row_absent(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        result = await get_cached_track_streaming_url(
            pg,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist="Juana Molina",
            album="DOGA",
            song="la paradoja",
        )
        assert result is None

    async def test_returns_url_on_hit(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": _SAMPLE_URL})

        result = await get_cached_track_streaming_url(
            pg,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist="Juana Molina",
            album="DOGA",
            song="la paradoja",
        )
        assert result == _SAMPLE_URL

    async def test_select_is_keyed_on_the_song_axis(self):
        # The SELECT must bind the normalized song as the fourth key so a
        # DIFFERENT song of the same album cannot match (no wrong-track).
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        await get_cached_track_streaming_url(
            pg,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist="Juana Molina",
            album="DOGA",
            song="la paradoja",
        )

        sql, *binds = pg.fetchone.await_args.args
        assert "song_normalized" in sql
        assert binds == [
            APPLE_MUSIC_TRACK_SERVICE,
            to_match_form("Juana Molina"),
            to_match_form("DOGA"),
            to_match_form("la paradoja"),
        ]

    async def test_none_album_normalizes_to_empty_string(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        await get_cached_track_streaming_url(
            pg,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist="Juana Molina",
            album=None,
            song="la paradoja",
        )
        _, _service, _artist, album_key, _song = pg.fetchone.await_args.args
        assert album_key == ""

    async def test_pg_error_degrades_to_none(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(side_effect=RuntimeError("pg down"))

        result = await get_cached_track_streaming_url(
            pg,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist="Juana Molina",
            album="DOGA",
            song="la paradoja",
        )
        assert result is None


@pytest.mark.asyncio
class TestSetCachedTrackStreamingURL:
    async def test_upserts_with_normalized_keys(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_track_streaming_url(
            pg,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist="Juana Molina",
            album="DOGA",
            song="la paradoja",
            url=_SAMPLE_URL,
        )

        sql, *binds = pg.execute.await_args.args
        assert "INSERT INTO lml_cache.track_streaming_url_cache" in sql
        assert "ON CONFLICT" in sql
        assert binds == [
            APPLE_MUSIC_TRACK_SERVICE,
            to_match_form("Juana Molina"),
            to_match_form("DOGA"),
            to_match_form("la paradoja"),
            _SAMPLE_URL,
        ]

    async def test_write_failure_is_swallowed(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=RuntimeError("pg down"))

        # Best-effort: a write failure must not propagate.
        await set_cached_track_streaming_url(
            pg,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist="Juana Molina",
            album="DOGA",
            song="la paradoja",
            url=_SAMPLE_URL,
        )
