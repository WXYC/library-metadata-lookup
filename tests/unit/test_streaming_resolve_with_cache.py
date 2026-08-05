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

import asyncio
from unittest.mock import AsyncMock, create_autospec

import httpx
import pytest

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.base import BaseStreamingClient
from clients.streaming.spotify import SpotifyClient
from entity.sources import PgSource
from entity.streaming_url_cache import ResolveOutcome, resolve_streaming_url_with_cache
from streaming.models import SourceMatch

_SERVICE_CASES = [
    ("apple_music_album", "https://music.apple.com/us/album/foo/1234567890"),
    ("spotify_album", "https://open.spotify.com/album/1A2GTWGt0LBTGQAyA3OKAf"),
]

_AUTOCOMPLETE_URL = "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"


def _match(url: str) -> SourceMatch:
    return SourceMatch(url=url, confidence=95.0)


class _InstantLimiter:
    """A rate limiter stand-in with a no-op ``acquire`` -- see the identical
    fixture in ``test_bandcamp_fail_fast_probe.py``. Duplicated rather than
    imported per this repo's convention for private test helpers (that file's
    own docstring explains why); without it a real ``BandcampClient()``'s
    ``AsyncLimiter(1, 1)`` would pace the two sequential HTTP calls below
    (artist search, then the album-search fallback) ~1s apart for real.
    """

    async def acquire(self) -> None:
        return None


def _autocomplete_response(results: list[dict], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"results": results} if status_code == 200 else None,
        request=httpx.Request("GET", _AUTOCOMPLETE_URL),
    )


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
        pg.fetchone = AsyncMock(return_value={"url": None, "is_error": False})
        client = AsyncMock(spec=BaseStreamingClient)

        outcome = await resolve_streaming_url_with_cache(
            pg, client, service=service, artist="Sessa", album="Estrela Acesa"
        )

        assert outcome == ResolveOutcome(url=None, source="cache_miss_recent")
        client.find_album_match.assert_not_called()

    async def test_recent_error_row_returns_cache_error_recent_and_skips_live_call(
        self, service, sample_url
    ):
        # LML#1115: a fresh ERROR row means LML never got an answer -- it must
        # not collapse to the same ``cache_miss_recent`` source a genuine
        # confirmed-absent miss reports. The /lookup post-process keys its
        # status mapping and warm-enqueue posture off this distinction.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": None, "is_error": True})
        client = AsyncMock(spec=BaseStreamingClient)

        outcome = await resolve_streaming_url_with_cache(
            pg, client, service=service, artist="Sessa", album="Estrela Acesa"
        )

        assert outcome == ResolveOutcome(url=None, source="cache_error_recent")
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

    async def test_client_exception_returns_live_error_and_writes_short_ttl_error_row(
        self, service, sample_url
    ):
        # LML#1121: a client raising during the probe must not break the
        # request (still ``live_error``, distinct from ``live_miss``), but
        # UNLIKE the pre-#1121 posture it now DOES write a row -- marked
        # ``is_error=True`` so it ages on the short error TTL instead of the
        # 7-day miss TTL. This lets a *reader* tell a couldn't-ask apart from
        # a confirmed absence; it does NOT throttle the default /lookup warm
        # path's re-enqueue rate (that path deliberately bypasses a fresh
        # error row -- see the canonical rationale in
        # lookup/streaming_url_postprocess.py). LML#1094/#1141.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        client = AsyncMock(spec=BaseStreamingClient)
        client.find_album_match = AsyncMock(side_effect=RuntimeError("upstream flake"))

        outcome = await resolve_streaming_url_with_cache(
            pg, client, service=service, artist="Hyd", album="Hold Onto Me Infinity"
        )

        assert outcome == ResolveOutcome(url=None, source="live_error")
        pg.execute.assert_awaited_once()
        call = pg.execute.await_args
        assert call.args[4] is None
        assert call.args[5] is True

    async def test_external_timeout_cancels_before_upsert_no_poison(self, service, sample_url):
        # The load-bearing safety property (LML#573): the per-call ceiling lives
        # at the post-process gather level, so a wait_for timeout cancels the
        # REAL resolver mid-probe. CancelledError is a BaseException, so the
        # resolver's `except Exception` does NOT swallow it — it propagates, and
        # the cancellation interrupts before any UPSERT. Exercises the actual
        # resolver (not a stubbed one) to lock the "don't poison the cache on
        # timeout" posture against a future `except BaseException` slip.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock()
        client = AsyncMock(spec=BaseStreamingClient)

        async def hang(*args, **kwargs):
            await asyncio.sleep(10)

        client.find_album_match = AsyncMock(side_effect=hang)

        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await asyncio.wait_for(
                resolve_streaming_url_with_cache(
                    pg, client, service=service, artist="Hyd", album="Hold Onto Me Infinity"
                ),
                timeout=0.05,
            )

        # No cache write — the probe was cancelled before reaching any UPSERT.
        pg.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("service", "sample_url"), _SERVICE_CASES)
class TestResolveStreamingURLWithCacheFailFast:
    """LML#1106: ``fail_fast=True`` is the mode a future synchronous Bandcamp
    live probe (LML#1098) is expected to use. It changes exactly two things
    relative to the default mode: the ``fail_fast`` kwarg is forwarded to
    ``client.find_album_match``, and a live-probe exception is RE-RAISED
    instead of being swallowed to ``live_error`` -- so a caller that wants
    sharp failure semantics (to tell a breaker-tripping shed apart from a
    generic upstream flake) can see it. Cache-hit and cache-miss-recent
    short-circuit identically either way -- ``fail_fast`` only ever changes
    what happens once a live call is needed.
    """

    async def test_cache_hit_still_short_circuits_without_touching_client(
        self, service, sample_url
    ):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": sample_url})
        client = AsyncMock(spec=BaseStreamingClient)

        outcome = await resolve_streaming_url_with_cache(
            pg,
            client,
            service=service,
            artist="Hyd",
            album="Hold Onto Me Infinity",
            fail_fast=True,
        )

        assert outcome == ResolveOutcome(url=sample_url, source="cache_hit")
        client.find_album_match.assert_not_called()

    async def test_live_call_forwards_fail_fast_kwarg(self, service, sample_url):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        client = AsyncMock(spec=BaseStreamingClient)
        client.find_album_match = AsyncMock(return_value=_match(sample_url))

        outcome = await resolve_streaming_url_with_cache(
            pg,
            client,
            service=service,
            artist="Hyd",
            album="Hold Onto Me Infinity",
            fail_fast=True,
        )

        assert outcome.source == "live_resolved"
        client.find_album_match.assert_awaited_once_with(
            "Hyd", "Hold Onto Me Infinity", fail_fast=True
        )

    async def test_live_call_exception_propagates_without_cache_write(self, service, sample_url):
        # Unlike the default mode (test_client_exception_returns_live_error_
        # without_cache_write above), fail_fast re-raises so the caller (a
        # breaker-aware live probe) can distinguish a shed from a generic
        # error. Either way, no cache write on the exception path.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock()
        client = AsyncMock(spec=BaseStreamingClient)
        client.find_album_match = AsyncMock(side_effect=RuntimeError("shed"))

        with pytest.raises(RuntimeError, match="shed"):
            await resolve_streaming_url_with_cache(
                pg,
                client,
                service=service,
                artist="Hyd",
                album="Hold Onto Me Infinity",
                fail_fast=True,
            )

        pg.execute.assert_not_called()

    async def test_live_miss_still_records_null(self, service, sample_url):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        client = AsyncMock(spec=BaseStreamingClient)
        client.find_album_match = AsyncMock(return_value=None)

        outcome = await resolve_streaming_url_with_cache(
            pg,
            client,
            service=service,
            artist="ObscureArtist",
            album="ObscureAlbum",
            fail_fast=True,
        )

        assert outcome == ResolveOutcome(url=None, source="live_miss")
        pg.execute.assert_awaited_once()


@pytest.mark.asyncio
class TestResolveStreamingURLWithCacheFailFastClientShape:
    """LML#1106 review finding 4: ``fail_fast`` is accepted ONLY by
    ``BandcampClient`` -- ``clients/streaming/base.py``'s ``find_album_match``
    and every other concrete client (Apple Music, Spotify, YouTube Music,
    Deezer) take no ``**kwargs``. The ``AsyncMock(spec=BaseStreamingClient)``
    doubles ``TestResolveStreamingURLWithCacheFailFast`` uses above restrict
    attribute NAMES only -- child mocks accept any call signature -- so
    ``test_live_call_forwards_fail_fast_kwarg`` there certifies green
    precisely the two combinations (apple_music_album, spotify_album) that
    raise ``TypeError`` in production, while Bandcamp itself is never
    exercised. These use signature-checked doubles (``create_autospec``) to
    close that gap: one pin that the ONE real combination that works keeps
    working, and one pin that the others fail loudly with no cache write
    (the current, verified behavior) instead of only "passing" a call shape
    that was never real.
    """

    async def test_live_call_forwards_fail_fast_kwarg_to_bandcamp_client(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        client = create_autospec(BandcampClient, instance=True)
        client.find_album_match.return_value = _match("https://x.bandcamp.com/album/y")

        outcome = await resolve_streaming_url_with_cache(
            pg,
            client,
            service="bandcamp",
            artist="Autechre",
            album="Confield",
            fail_fast=True,
        )

        assert outcome.source == "live_resolved"
        assert outcome.url == "https://x.bandcamp.com/album/y"
        client.find_album_match.assert_awaited_once_with("Autechre", "Confield", fail_fast=True)
        pg.execute.assert_awaited_once()
        assert pg.execute.await_args.args[1] == "bandcamp"

    @pytest.mark.parametrize(
        ("service", "client_class"),
        [
            ("apple_music_album", AppleMusicClient),
            ("spotify_album", SpotifyClient),
        ],
    )
    async def test_fail_fast_with_a_non_bandcamp_client_fails_loudly_no_cache_write(
        self, service, client_class
    ):
        # create_autospec validates the call signature against the REAL
        # class -- none of these clients' find_album_match accepts
        # fail_fast, so the call itself raises TypeError before anything is
        # awaited. resolve_streaming_url_with_cache's fail_fast branch
        # re-raises (rather than swallowing to live_error), so this
        # surfaces loudly, with no cache write either way.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock()
        client = create_autospec(client_class, instance=True)

        with pytest.raises(TypeError, match="fail_fast"):
            await resolve_streaming_url_with_cache(
                pg,
                client,
                service=service,
                artist="Autechre",
                album="Confield",
                fail_fast=True,
            )

        pg.execute.assert_not_called()


@pytest.mark.asyncio
class TestResolveStreamingURLWithCacheBandcampDefaultPath:
    """LML#1115: on the default (non-``fail_fast``) path, ``BandcampClient``
    used to swallow a transport failure (5xx, Cloudflare 403/1015, connect
    timeout) to a clean ``None`` well before it ever reached this resolver --
    indistinguishable from a genuine no-match, so the ``if match is None:``
    branch below would UPSERT a 7-day known-miss row for an outage. LML#1115
    fixed that at the client layer (``clients/bandcamp.py``); these tests
    exercise the REAL ``BandcampClient`` (mocked only at the HTTP transport)
    through this resolver end-to-end, rather than a stand-in that always
    agreed with whatever the client layer's contract used to be.

    LML#1121 review: dropping the write entirely also left no row for a
    *reader* to consult (the dedup key in ``lookup/streaming_warm.py`` is
    discarded once the warm task finishes, so it dedups concurrent warms
    only) -- for as long as an outage lasts, every subsequent /lookup for the
    same album re-enqueues a fresh warm, unchanged by this fix. The fix keeps
    #1115's guarantee (a transport failure never freezes as a 7-day
    known-miss) and, via a short, env-tunable error TTL (``is_error=True``,
    see ``TestResolveStreamingErrorTTL`` in ``test_streaming_url_cache.py``),
    lets a reader tell a couldn't-ask apart from a confirmed absence -- it
    does NOT restore backpressure on the default /lookup warm path (see the
    canonical rationale in ``lookup/streaming_url_postprocess.py``; LML#1141
    tracks that follow-up).
    """

    async def test_transport_failure_returns_live_error_and_writes_short_ttl_error_row(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        client = BandcampClient()
        client._rate_limiter = _InstantLimiter()  # type: ignore[assignment]
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_autocomplete_response([], status_code=503))

        outcome = await resolve_streaming_url_with_cache(
            pg, client, service="bandcamp", artist="Sessa", album="Estrela Acesa"
        )

        assert outcome == ResolveOutcome(url=None, source="live_error")
        pg.execute.assert_awaited_once()
        call = pg.execute.await_args
        assert call.args[4] is None
        assert call.args[5] is True

    async def test_genuine_no_match_still_writes_known_miss(self):
        # The ticket's "keep this" requirement: a real 200-with-no-match must
        # still write the negative-cache row with the existing TTL -- LML#1115
        # must not weaken this deliberate negative caching.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        pg.execute = AsyncMock(return_value="INSERT 0 1")
        client = BandcampClient()
        client._rate_limiter = _InstantLimiter()  # type: ignore[assignment]
        client._http = AsyncMock(spec=httpx.AsyncClient)
        client._http.request = AsyncMock(return_value=_autocomplete_response([]))

        outcome = await resolve_streaming_url_with_cache(
            pg,
            client,
            service="bandcamp",
            artist="Nonexistent Artist",
            album="Nonexistent Album",
        )

        assert outcome == ResolveOutcome(url=None, source="live_miss")
        pg.execute.assert_awaited_once()
        assert pg.execute.await_args.args[4] is None
        # LML#1121: a genuine miss is NOT an error row -- it ages on the
        # 7-day miss TTL, not the short error TTL.
        assert pg.execute.await_args.args[5] is False
