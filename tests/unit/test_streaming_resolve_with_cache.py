"""Unit tests for the cache-backed streaming-URL resolver.

``resolve_streaming_url_with_cache`` is the per-service building block the
``/api/v1/lookup`` post-process calls when an item's service URL came out
null. It applies a read-through cache (PG-backed) over a live
``BaseStreamingClient.find_album_match`` probe using the REQUEST's
``(artist, album)`` — independent of any library row.

It returns ``ResolveOutcome(url, source)`` where ``source`` is one of
``cache_hit``, ``cache_miss_recent``, ``live_resolved``, ``live_miss``, or
``live_error``. LML#573 moved the per-call ``asyncio.wait_for`` ceiling OUT
of this resolver and up to the post-process gather level (per-service
``probe_timeout_s``), so the resolver no longer takes a ``probe_timeout_s``
parameter. A client exception still surfaces as ``live_error`` with NO cache
write; an external timeout (``wait_for`` at the gather) cancels the resolver
before its UPSERT, which the post-process maps to ``live_error`` too.

PG and the streaming client are mocked. Integration tests cover the SQL.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from clients.streaming.base import BaseStreamingClient
from entity.sources import PgSource
from entity.streaming_url_cache import ResolveOutcome, resolve_streaming_url_with_cache
from streaming.models import SourceMatch

_SERVICE_CASES = [
    ("apple_music_album", "https://music.apple.com/us/album/foo/1234567890"),
    ("spotify_album", "https://open.spotify.com/album/1A2GTWGt0LBTGQAyA3OKAf"),
]


def _match(url: str) -> SourceMatch:
    return SourceMatch(url=url, confidence=95.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(("service", "sample_url"), _SERVICE_CASES)
class TestResolveStreamingURLWithCache:
    async def test_cache_hit_short_circuits_live_call(self, service, sample_url):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": sample_url})
        client = AsyncMock(spec=BaseStreamingClient)

        outcome = await resolve_streaming_url_with_cache(
            pg, client, service=service, artist="Hyd", album="Hold Onto Me Infinity"
        )

        assert outcome == ResolveOutcome(url=sample_url, source="cache_hit")
        client.find_album_match.assert_not_called()
        # No cache write — a hit doesn't refresh the row.
        pg.execute.assert_not_called()

    async def test_recent_known_miss_skips_live_call(self, service, sample_url):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": None})
        client = AsyncMock(spec=BaseStreamingClient)

        outcome = await resolve_streaming_url_with_cache(
            pg, client, service=service, artist="Sessa", album="Estrela Acesa"
        )

        assert outcome == ResolveOutcome(url=None, source="cache_miss_recent")
        client.find_album_match.assert_not_called()

    async def test_no_cache_row_calls_client_with_request_values(self, service, sample_url):
        # No row → live probe with REQUEST artist/album (NOT a fallback row's
        # artist) — this is the Hyd-shape fix.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        client = AsyncMock(spec=BaseStreamingClient)
        client.find_album_match = AsyncMock(return_value=_match(sample_url))

        outcome = await resolve_streaming_url_with_cache(
            pg, client, service=service, artist="Hyd", album="Hold Onto Me Infinity"
        )

        assert outcome.url == sample_url
        assert outcome.source == "live_resolved"
        client.find_album_match.assert_awaited_once_with("Hyd", "Hold Onto Me Infinity")
        # Result is persisted under the right service key.
        pg.execute.assert_awaited_once()
        upsert_sql = pg.execute.await_args.args[0]
        assert "INSERT INTO lml_cache.album_streaming_url_cache" in upsert_sql
        assert pg.execute.await_args.args[1] == service

    async def test_stale_known_miss_falls_through_to_live(self, service, sample_url):
        # LML#576: a stale known miss is filtered out by the SQL WHERE so the
        # cache returns no row; the resolver treats it like an absent entry.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        client = AsyncMock(spec=BaseStreamingClient)
        client.find_album_match = AsyncMock(return_value=_match(sample_url))

        outcome = await resolve_streaming_url_with_cache(
            pg, client, service=service, artist="Sessa", album="Estrela Acesa"
        )

        assert outcome.url == sample_url
        assert outcome.source == "live_resolved"
        pg.execute.assert_awaited_once()

    async def test_live_miss_records_null(self, service, sample_url):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        client = AsyncMock(spec=BaseStreamingClient)
        client.find_album_match = AsyncMock(return_value=None)

        outcome = await resolve_streaming_url_with_cache(
            pg, client, service=service, artist="ObscureArtist", album="ObscureAlbum"
        )

        assert outcome == ResolveOutcome(url=None, source="live_miss")
        pg.execute.assert_awaited_once()
        # url bound as NULL so subsequent requests inside the TTL short-circuit.
        assert pg.execute.await_args.args[4] is None

    async def test_uses_find_album_match_not_track_metadata(self, service, sample_url):
        # Regression pin: the resolver must use find_album_match so the cache
        # stores album URLs (not per-track deep-links).
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        client = AsyncMock(spec=BaseStreamingClient)
        client.find_album_match = AsyncMock(return_value=_match(sample_url))
        client.find_track_metadata = AsyncMock()

        await resolve_streaming_url_with_cache(
            pg, client, service=service, artist="Cat Power", album="Moon Pix"
        )

        client.find_album_match.assert_awaited_once()
        client.find_track_metadata.assert_not_called()

    async def test_client_exception_returns_live_error_without_cache_write(
        self, service, sample_url
    ):
        # A client raising during the probe must not break the request.
        # Treated as live_error (distinct from live_miss): NO cache write so
        # the next request retries rather than locking in a spurious null.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock()
        client = AsyncMock(spec=BaseStreamingClient)
        client.find_album_match = AsyncMock(side_effect=RuntimeError("upstream flake"))

        outcome = await resolve_streaming_url_with_cache(
            pg, client, service=service, artist="Hyd", album="Hold Onto Me Infinity"
        )

        assert outcome == ResolveOutcome(url=None, source="live_error")
        pg.execute.assert_not_called()
