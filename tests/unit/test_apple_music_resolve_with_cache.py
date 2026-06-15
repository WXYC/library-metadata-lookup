"""Unit tests for the cache-backed Apple Music URL resolver.

``resolve_apple_music_url_with_cache`` is the building block the
``/api/v1/lookup`` post-process calls when an item's ``apple_music_url``
came out null. It applies a read-through cache (PG-backed) over a live
``AppleMusicClient.find_album_match`` probe using the REQUEST's
``(artist, album)`` — independent of any library row.

The function returns ``ResolveOutcome(url, source)`` where ``source`` is
one of ``"cache_hit"``, ``"cache_miss_recent"`` (known miss inside TTL),
``"live_resolved"`` (Apple returned a URL), ``"live_miss"`` (Apple
returned no match), or ``"live_error"`` (Apple raised / probe timed
out). The caller branches on the URL; dashboards branch on the source.

LML#576 moved staleness from a Python check on ``last_checked_at`` into
the SQL ``WHERE`` clause. Mocked SELECT rows are now ``{"apple_music_url":
...}`` only — no ``last_checked_at`` field — and a stale miss is
represented by the mock returning ``None`` from ``fetchone`` (the SQL
filter would have excluded the row in production).

PG and the Apple client are mocked. Integration tests cover the SQL.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from clients.streaming.apple_music import AppleMusicClient
from entity.apple_music_album_cache import (
    ResolveOutcome,
    resolve_apple_music_url_with_cache,
)
from entity.sources import PgSource
from streaming.models import SourceMatch


def _album_match(url: str = "https://music.apple.com/us/album/foo/1234567890") -> SourceMatch:
    return SourceMatch(url=url, confidence=95.0)


@pytest.mark.asyncio
class TestResolveAppleMusicURLWithCache:
    async def test_cache_hit_short_circuits_apple_call(self):
        # Cache has a row for (artist, album) with a non-null URL → use it
        # without touching Apple's API.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(
            return_value={"apple_music_url": "https://music.apple.com/us/album/cached/9"}
        )
        apple_music = AsyncMock(spec=AppleMusicClient)

        outcome = await resolve_apple_music_url_with_cache(
            pg,
            apple_music,
            artist="Hyd",
            album="Hold Onto Me Infinity",
        )

        assert outcome == ResolveOutcome(
            url="https://music.apple.com/us/album/cached/9", source="cache_hit"
        )
        # Apple client never called.
        apple_music.find_album_match.assert_not_called()
        # No cache write either — hit doesn't refresh the row.
        pg.execute.assert_not_called()

    async def test_recent_known_miss_skips_apple_call(self):
        # Cache has a row with apple_music_url=NULL and last_checked_at recent
        # → the SQL filter let it through → respect the recorded miss, don't
        # re-query Apple.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"apple_music_url": None})
        apple_music = AsyncMock(spec=AppleMusicClient)

        outcome = await resolve_apple_music_url_with_cache(
            pg, apple_music, artist="Sessa", album="Estrela Acesa"
        )

        assert outcome == ResolveOutcome(url=None, source="cache_miss_recent")
        apple_music.find_album_match.assert_not_called()

    async def test_stale_known_miss_falls_through_to_apple(self):
        # LML#576: a stale known miss is filtered out by the SQL WHERE
        # (``last_checked_at <= now() - miss_ttl``) so the cache returns
        # no row. The resolver treats this the same as an absent entry
        # and calls Apple — no Python-side staleness check survives.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_album_match = AsyncMock(return_value=_album_match())

        outcome = await resolve_apple_music_url_with_cache(
            pg, apple_music, artist="Sessa", album="Estrela Acesa"
        )

        # The stale miss path looks indistinguishable from an absent row
        # at this layer — both end as ``live_resolved`` after Apple
        # returns a URL, and the UPSERT refreshes the stale row in place.
        assert outcome.url == "https://music.apple.com/us/album/foo/1234567890"
        assert outcome.source == "live_resolved"
        apple_music.find_album_match.assert_awaited_once_with("Sessa", "Estrela Acesa")
        pg.execute.assert_awaited_once()

    async def test_no_cache_row_calls_apple_with_request_values(self):
        # No row → live probe with REQUEST artist/album (NOT a fallback
        # row's artist) — this is the Hyd-shape fix.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_album_match = AsyncMock(return_value=_album_match())

        outcome = await resolve_apple_music_url_with_cache(
            pg,
            apple_music,
            artist="Hyd",
            album="Hold Onto Me Infinity",
        )

        assert outcome.url == "https://music.apple.com/us/album/foo/1234567890"
        assert outcome.source == "live_resolved"
        # Apple called with REQUEST values, positionally.
        apple_music.find_album_match.assert_awaited_once_with("Hyd", "Hold Onto Me Infinity")
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
        apple_music.find_album_match = AsyncMock(return_value=None)

        outcome = await resolve_apple_music_url_with_cache(
            pg,
            apple_music,
            artist="ObscureArtist",
            album="ObscureAlbum",
        )

        assert outcome == ResolveOutcome(url=None, source="live_miss")
        # Cache write with NULL url (the bind value).
        pg.execute.assert_awaited_once()
        bind_url = pg.execute.await_args.args[3]
        assert bind_url is None

    async def test_uses_find_album_match_not_track_metadata(self):
        # Regression pin: the resolver must use find_album_match so the
        # cache stores album URLs (not per-track ?i= deep-links). A
        # song-level URL keyed by (artist, album) is a wrong-track leak
        # when a later request asks about a different song.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_album_match = AsyncMock(return_value=_album_match())
        # Ensure the resolver does NOT touch find_track_metadata.
        apple_music.find_track_metadata = AsyncMock()

        await resolve_apple_music_url_with_cache(
            pg, apple_music, artist="Cat Power", album="Moon Pix"
        )

        apple_music.find_album_match.assert_awaited_once()
        apple_music.find_track_metadata.assert_not_called()

    async def test_apple_exception_does_not_propagate_and_returns_live_error(self):
        # Apple client raising during the post-process must not break the
        # request. Treated as live_error (distinct from live_miss): no
        # cache write so the next request tries again rather than locking
        # in a spurious null.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock()
        apple_music = AsyncMock(spec=AppleMusicClient)
        apple_music.find_album_match = AsyncMock(side_effect=RuntimeError("apple flake"))

        outcome = await resolve_apple_music_url_with_cache(
            pg,
            apple_music,
            artist="Hyd",
            album="Hold Onto Me Infinity",
        )

        # live_error so dashboards distinguish outage from catalog miss.
        assert outcome == ResolveOutcome(url=None, source="live_error")
        # NO cache write — transient errors must not poison the cache.
        pg.execute.assert_not_called()

    async def test_probe_timeout_caps_live_call(self):
        # An Apple call that hangs longer than probe_timeout_s must be
        # interrupted and reported as live_error — the in-line probe has
        # the same posture via LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock()
        apple_music = AsyncMock(spec=AppleMusicClient)

        async def hang(*args, **kwargs):
            await asyncio.sleep(10)

        apple_music.find_album_match = AsyncMock(side_effect=hang)

        outcome = await resolve_apple_music_url_with_cache(
            pg,
            apple_music,
            artist="Hyd",
            album="Hold Onto Me Infinity",
            probe_timeout_s=0.05,
        )

        assert outcome == ResolveOutcome(url=None, source="live_error")
        # NO cache write — timeouts are transient, don't poison.
        pg.execute.assert_not_called()
