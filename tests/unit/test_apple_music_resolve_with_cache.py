"""Unit tests for the cache-backed Apple Music URL resolver.

``resolve_apple_music_url_with_cache`` is the building block the
``/api/v1/lookup`` post-process calls when an item's ``apple_music_url``
came out null. It applies a read-through cache (PG-backed) over a live
``AppleMusicClient.find_track_metadata`` probe using the REQUEST's
``(artist, album, song)`` — independent of any library row.

The function returns ``ResolveOutcome(url, source)`` where ``source`` is
one of ``"cache_hit"``, ``"cache_miss_recent"`` (known miss inside TTL),
``"live_resolved"`` (Apple returned a URL), ``"live_miss"`` (Apple
returned no match). The caller branches on the URL and tags the
``source`` onto the Sentry transaction.

PG and the Apple client are mocked. Integration tests cover the SQL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from clients.streaming.apple_music import AppleMusicClient, AppleMusicTrackMatch
from entity.apple_music_album_cache import (
    ResolveOutcome,
    resolve_apple_music_url_with_cache,
)
from entity.sources import PgSource


def _track_match(url: str = "https://music.apple.com/us/album/foo/1234567890"):
    return AppleMusicTrackMatch(url=url, artwork_url=None, release_year=None)


@pytest.mark.asyncio
class TestResolveAppleMusicURLWithCache:
    async def test_cache_hit_short_circuits_apple_call(self):
        # Cache has a row for (artist, album) with a non-null URL → use it
        # without touching Apple's API.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(
            return_value={
                "apple_music_url": "https://music.apple.com/us/album/cached/9",
                "last_checked_at": datetime.now(UTC),
            }
        )
        apple_music = AsyncMock(spec=AppleMusicClient)

        outcome = await resolve_apple_music_url_with_cache(
            pg,
            apple_music,
            artist="Hyd",
            album="Hold Onto Me Infinity",
            song="Angel",
        )

        assert outcome == ResolveOutcome(
            url="https://music.apple.com/us/album/cached/9", source="cache_hit"
        )
        # Apple client never called.
        apple_music.find_track_metadata.assert_not_called()
        # No cache write either — hit doesn't refresh the row.
        pg.execute.assert_not_called()

    async def test_recent_known_miss_skips_apple_call(self):
        # Cache has a row with apple_music_url=NULL and last_checked_at recent
        # → respect the recorded miss, don't re-query Apple.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(
            return_value={
                "apple_music_url": None,
                "last_checked_at": datetime.now(UTC),
            }
        )
        apple_music = AsyncMock(spec=AppleMusicClient)

        outcome = await resolve_apple_music_url_with_cache(
            pg, apple_music, artist="Sessa", album="Estrela Acesa", song=None
        )

        assert outcome == ResolveOutcome(url=None, source="cache_miss_recent")
        apple_music.find_track_metadata.assert_not_called()

    async def test_no_cache_row_calls_apple_with_request_values(self):
        # No row → live probe with REQUEST artist/album/song (NOT a fallback
        # row's artist) — this is the Hyd-shape fix.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=_track_match())

        outcome = await resolve_apple_music_url_with_cache(
            pg,
            apple_music,
            artist="Hyd",
            album="Hold Onto Me Infinity",
            song="Angel",
        )

        assert outcome.url == "https://music.apple.com/us/album/foo/1234567890"
        assert outcome.source == "live_resolved"
        # Apple called with REQUEST values, positionally + by kwarg.
        apple_music.find_track_metadata.assert_awaited_once_with(
            "Hyd", "Angel", album="Hold Onto Me Infinity"
        )
        # Result is persisted: one UPSERT against the cache.
        pg.execute.assert_awaited_once()
        upsert_sql = pg.execute.await_args.args[0]
        assert "INSERT INTO entity.album_apple_music_lookup_cache" in upsert_sql

    async def test_apple_returns_none_records_miss(self):
        # Live probe miss → still write to cache (with url=None) so the next
        # request inside the TTL can short-circuit.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=None)

        outcome = await resolve_apple_music_url_with_cache(
            pg,
            apple_music,
            artist="ObscureArtist",
            album="ObscureAlbum",
            song="ObscureSong",
        )

        assert outcome == ResolveOutcome(url=None, source="live_miss")
        # Cache write with NULL url (the bind value).
        pg.execute.assert_awaited_once()
        bind_url = pg.execute.await_args.args[3]
        assert bind_url is None

    async def test_album_only_lookup_skips_song_argument(self):
        # When the caller has no song (e.g. iOS V2 album-detail panel), the
        # probe runs with the album only — find_track_metadata accepts
        # ``song=""`` but most natural is to pass empty string and rely on
        # the 80/80 floor against artist+album.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(return_value=_track_match())

        await resolve_apple_music_url_with_cache(
            pg, apple_music, artist="Cat Power", album="Moon Pix", song=None
        )

        # song=None becomes "" so the AppleMusicClient surface stays uniform.
        apple_music.find_track_metadata.assert_awaited_once_with("Cat Power", "", album="Moon Pix")

    async def test_apple_exception_does_not_propagate(self):
        # Apple client raising during the post-process must not break the
        # request. Treated as a miss; no cache write so the next request
        # tries again rather than locking in a spurious null.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock()
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_track_metadata = AsyncMock(side_effect=RuntimeError("apple flake"))

        outcome = await resolve_apple_music_url_with_cache(
            pg,
            apple_music,
            artist="Hyd",
            album="Hold Onto Me Infinity",
            song="Angel",
        )

        # We return live_miss with url=None — caller treats the same as a
        # genuine miss. No cache write (we don't want to record a transient
        # error as a permanent "checked and not found").
        assert outcome == ResolveOutcome(url=None, source="live_miss")
        pg.execute.assert_not_called()
