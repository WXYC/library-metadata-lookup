"""Unit tests for LML#1106's Bandcamp fail-fast mode.

``BandcampClient.find_album_match(..., fail_fast=True)`` is the mode a future
synchronous live probe (LML#1098) is expected to use: a single attempt per
HTTP call (no ``discogs.admission.retry_429`` backoff loop -- inline latency
must not stack a 3s+ retry), a 429 raises ``BandcampRateLimitedError`` instead
of silently degrading to "no match" (so a caller can distinguish a shed from
a genuine miss and never poison a negative cache), and the whole call is
gated by ``BandcampProbeBreaker`` so a sustained 429 run sheds immediately
without ever touching the network.

The default (``fail_fast=False``) path's 429-retry-loop mechanics
(``_request_with_retry``'s ``discogs.admission.retry_429`` backoff) are
untouched -- pinned here (``test_default_mode_unaffected`` below) so a
future edit can't silently change the retrying background-warm/offline-drain
behavior other callers depend on. LML#1115 separately extends the
NON-429-transport-failure raise (``BandcampTransportError`` /
``BandcampSearchUnavailableError``) to that same default path one layer up
(``search_artist`` / ``fetch_artist_catalog`` / ``search_albums`` /
``find_album_match_via_search`` / ``_find_album_match_impl`` in
``clients/bandcamp.py``) -- see ``tests/unit/test_bandcamp_client.py`` and
``tests/unit/test_bandcamp_album_search.py`` for that coverage.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from clients.bandcamp import BandcampClient, BandcampRateLimitedError, BandcampTransportError
from clients.bandcamp_breaker import (
    BandcampBreakerOpenError,
    BandcampBreakerState,
    BandcampProbeBreaker,
    get_bandcamp_probe_breaker,
)

_URL = "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"


class _InstantLimiter:
    """A rate limiter stand-in with a no-op ``acquire`` -- see the identical
    fixture in ``test_bandcamp_retry_characterization.py``. Duplicated rather
    than imported: that file is pinned unmodified (LML#1106 review scope),
    and cross-importing a private test helper across sibling modules isn't
    this repo's convention.

    LML#1106 review FIX 7: without this, the breaker-integration tests below
    (several real ``find_album_match`` calls per test) pay ``AsyncLimiter(1,
    1)``'s real per-call pacing -- patching ``clients.bandcamp.asyncio.sleep``
    does NOT help, since aiolimiter paces via ``loop.call_later``, not
    ``asyncio.sleep``.
    """

    async def acquire(self) -> None:
        return None


def _response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=httpx.Request("GET", _URL))


@pytest.fixture(autouse=True)
def _reset_breaker():
    from clients.bandcamp_breaker import reset_bandcamp_probe_breaker

    reset_bandcamp_probe_breaker()
    yield
    reset_bandcamp_probe_breaker()


@pytest.fixture
def client() -> BandcampClient:
    c = BandcampClient()
    c._http = AsyncMock(spec=httpx.AsyncClient)
    c._rate_limiter = _InstantLimiter()  # type: ignore[assignment]
    return c


class TestFailFastSingleAttempt:
    @pytest.mark.asyncio
    async def test_429_raises_without_retrying(self, client):
        client._http.request = AsyncMock(return_value=_response(429))

        with pytest.raises(BandcampRateLimitedError):
            await client._request_with_retry("GET", _URL, fail_fast=True)

        # Exactly one attempt -- no backoff-and-retry loop.
        assert client._http.request.await_count == 1

    @pytest.mark.asyncio
    async def test_success_returns_response_without_touching_retry_loop(self, client):
        client._http.request = AsyncMock(return_value=_response(200))

        result = await client._request_with_retry("GET", _URL, fail_fast=True)

        assert result is not None
        assert result.status_code == 200
        assert client._http.request.await_count == 1

    @pytest.mark.asyncio
    async def test_network_error_returns_none_not_rate_limited(self, client):
        client._http.request = AsyncMock(side_effect=httpx.ConnectError("boom"))

        result = await client._request_with_retry("GET", _URL, fail_fast=True)

        assert result is None
        assert client._http.request.await_count == 1

    @pytest.mark.asyncio
    async def test_default_mode_unaffected(self, client):
        # fail_fast defaults False -- the pre-existing retry behavior is untouched.
        client._http.request = AsyncMock(side_effect=[_response(429), _response(200)])

        with patch("clients.bandcamp.asyncio.sleep", new=AsyncMock()):
            result = await client._request_with_retry("GET", _URL)

        assert result is not None
        assert result.status_code == 200
        assert client._http.request.await_count == 2


class TestFailFastCatalogFailureFallsThroughToAlbumSearch:
    """LML#1106 review FIX 5: ``_find_album_match_impl`` does ``catalog =
    await self.fetch_artist_catalog(..., fail_fast=fail_fast) or []``.
    Default mode: a non-200 returns ``None`` -> empty catalog -> falls
    through to ``find_album_match_via_search`` (the #1069 album-first
    fallback that recovers label/imprint-hosted releases). Under
    ``fail_fast`` the same response instead RAISES
    ``BandcampTransportError``, which previously escaped uncaught -- the
    fallback never ran. FIX 5 caught that leg's failure inside
    ``_find_album_match_impl`` so the album-first search still runs -- but
    FIX 5 also unconditionally recorded whatever the fallback returned as
    the OVERALL outcome, including a clean miss, which conflates "the
    lower-recall fallback found nothing" with "asked, got nothing": the
    album-autocomplete index has strictly lower recall than the catalog
    scrape (that is why #1069 added it as a fallback, not a replacement), so
    it finding nothing after the catalog leg couldn't even be consulted is
    NOT evidence of absence (LML#1106 review round 2, FIX A). These pin the
    corrected 3x2 table: a catalog-leg failure whose fallback HITS still
    resolves the match (we got an answer); a catalog-leg failure whose
    fallback comes back CLEAN now raises ``BandcampTransportError`` instead
    of resolving to a cachable ``None``; a catalog-leg failure whose
    fallback ALSO fails transport-wise still raises (unchanged)."""

    @pytest.mark.asyncio
    async def test_catalog_failure_with_clean_fallback_miss_raises_transport_error(self, client):
        # 1: search_artist (autocomplete) -> 200, a matching artist.
        # 2: fetch_artist_catalog (/music) -> 500 (BandcampTransportError).
        # 3: search_albums (album-first fallback) -> 200, no results.
        client._http.request = AsyncMock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "type": "b",
                                "name": "Autechre",
                                "url": "https://autechre.bandcamp.com",
                            }
                        ]
                    },
                    request=httpx.Request("GET", _URL),
                ),
                _response(500),
                httpx.Response(200, json={"results": []}, request=httpx.Request("GET", _URL)),
            ]
        )
        breaker = get_bandcamp_probe_breaker()

        with (
            patch.object(breaker, "record_success", wraps=breaker.record_success) as record_success,
            patch.object(
                breaker, "record_transport_failure", wraps=breaker.record_transport_failure
            ) as record_transport_failure,
        ):
            with pytest.raises(BandcampTransportError):
                await client.find_album_match("Autechre", "Confield", fail_fast=True)

        # All three legs fired -- the catalog failure did not pre-empt the
        # album-first fallback.
        assert client._http.request.await_count == 3
        # The catalog leg genuinely couldn't ask, and the lower-recall
        # fallback's clean miss is not evidence of absence -- this is a
        # couldn't-ask, not a genuine no-match, so it must be recorded as a
        # transport failure (counts toward opening the breaker), never a
        # success.
        record_transport_failure.assert_called_once_with(epoch=0)
        record_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_catalog_failure_falls_through_to_an_album_search_match(self, client):
        # Same shape, but the album-first fallback actually resolves a
        # match -- the label/imprint-hosted-release recovery #1069 exists
        # for, now reachable even when the artist's own catalog page 500s.
        # We got an answer, so this must still succeed and be recorded as a
        # genuine success, not a transport failure.
        client._http.request = AsyncMock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "type": "b",
                                "name": "Autechre",
                                "url": "https://autechre.bandcamp.com",
                            }
                        ]
                    },
                    request=httpx.Request("GET", _URL),
                ),
                _response(500),
                httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "type": "a",
                                "band_name": "Autechre",
                                "name": "Confield",
                                "url": "https://autechre.bandcamp.com/album/confield",
                            }
                        ]
                    },
                    request=httpx.Request("GET", _URL),
                ),
            ]
        )
        breaker = get_bandcamp_probe_breaker()

        with (
            patch.object(breaker, "record_success", wraps=breaker.record_success) as record_success,
            patch.object(
                breaker, "record_transport_failure", wraps=breaker.record_transport_failure
            ) as record_transport_failure,
        ):
            result = await client.find_album_match("Autechre", "Confield", fail_fast=True)

        assert result is not None
        assert result.url == "https://autechre.bandcamp.com/album/confield"
        assert client._http.request.await_count == 3
        record_success.assert_called_once_with(epoch=0)
        record_transport_failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_catalog_failure_then_album_search_failure_still_raises(self, client):
        # Preserve the invariant that a genuine couldn't-ask still raises
        # rather than returning a cachable ``None``: when BOTH the catalog
        # leg AND the album-first fallback fail transport-wise, the second
        # failure propagates.
        client._http.request = AsyncMock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "type": "b",
                                "name": "Autechre",
                                "url": "https://autechre.bandcamp.com",
                            }
                        ]
                    },
                    request=httpx.Request("GET", _URL),
                ),
                _response(500),
                _response(500),
            ]
        )

        with pytest.raises(BandcampTransportError):
            await client.find_album_match("Autechre", "Confield", fail_fast=True)

        assert client._http.request.await_count == 3


class TestFailFastTransportErrorRaiseSitesIndividually:
    """LML#1106 review FIX 7: ``search_artist`` / ``fetch_artist_catalog`` /
    ``search_albums`` each raise ``BandcampTransportError`` under
    ``fail_fast`` on their own non-200 response. Every existing
    ``find_album_match`` integration test drives a UNIFORM failing response
    (e.g. ``return_value=_response(500)`` for every call), so deleting any
    ONE of the three raise sites still leaves the OTHER TWO firing somewhere
    downstream -- the mutation survives undetected. These use MIXED
    per-leg responses (or call the method directly) to isolate each site."""

    @pytest.mark.asyncio
    async def test_search_artist_failure_is_not_masked_by_the_album_search_fallback(self, client):
        # search_artist's OWN response is a transport failure; the album-
        # search fallback response (were it ever reached) is a clean empty
        # result. If search_artist's raise were deleted (degrading to []
        # instead), best_artist would resolve to None and the call would
        # silently fall through to the fallback -- masking a genuine
        # Bandcamp outage on the very first leg as "no artist found".
        client._http.request = AsyncMock(
            side_effect=[
                _response(500),  # search_artist
                httpx.Response(200, json={"results": []}, request=httpx.Request("GET", _URL)),
            ]
        )

        with pytest.raises(BandcampTransportError):
            await client.find_album_match("Autechre", "Confield", fail_fast=True)

        # Exactly one call: the fallback must never run when the very first
        # leg genuinely couldn't ask.
        assert client._http.request.await_count == 1

    @pytest.mark.asyncio
    async def test_fetch_artist_catalog_raises_individually(self, client):
        # Direct call, bypassing find_album_match entirely: since FIX 5
        # (above) now catches this leg's BandcampTransportError and falls
        # through to the album-first search regardless of whether it was
        # raised or not, the raise is no longer independently observable
        # THROUGH find_album_match when the fallback resolves cleanly --
        # both branches converge on an empty catalog. Calling the method
        # directly is the only way left to pin this specific raise site.
        client._http.request = AsyncMock(return_value=_response(500))

        with pytest.raises(BandcampTransportError):
            await client.fetch_artist_catalog("autechre", fail_fast=True)

    @pytest.mark.asyncio
    async def test_search_albums_failure_propagates_not_swallowed_as_unavailable(self, client):
        # search_artist finds no match (a clean 200, empty) -> falls
        # through to the album-first fallback, whose OWN leg is a transport
        # failure. If search_albums's raise were deleted (degrading to
        # ``None`` instead), ``find_album_match_via_search`` would see
        # ``results is None`` and raise ``BandcampSearchUnavailableError``
        # instead -- which ``_find_album_match_impl`` explicitly catches
        # and converts to a clean ``None`` (the default-mode posture),
        # silently swallowing what should have been a couldn't-ask.
        client._http.request = AsyncMock(
            side_effect=[
                httpx.Response(200, json={"results": []}, request=httpx.Request("GET", _URL)),
                _response(500),  # search_albums (album-first fallback)
            ]
        )

        with pytest.raises(BandcampTransportError):
            await client.find_album_match("Obscure Artist", "Obscure Album", fail_fast=True)

        assert client._http.request.await_count == 2


class TestFailFastUnparseableJsonBody:
    """LML#1106 review FIX 6: ``search_artist`` / ``search_albums`` call
    ``resp.json()`` unguarded after the non-200 check. A 200 with a
    non-JSON body (a Cloudflare HTML interstitial, a truncated response)
    raises ``json.JSONDecodeError`` (a ``ValueError``), which escapes the
    fail-fast taxonomy entirely and lands in
    ``lookup/enrichment/bandcamp_probe.py``'s ``except Exception`` catch-all
    -- a loud ``logger.exception`` per item (the #755 flood shape) instead
    of the quiet, counted shed path. Catching ``ValueError`` around both
    ``.json()`` calls and raising ``BandcampTransportError`` under
    ``fail_fast`` routes this couldn't-ask through the same taxonomy as a
    5xx or a connect timeout. Default mode is untouched -- an unparseable
    200 still propagates exactly as it does today."""

    @staticmethod
    def _malformed_json_response() -> httpx.Response:
        # A 200 whose body isn't JSON at all -- the Cloudflare HTML
        # interstitial shape -- so ``.json()`` raises unconditionally.
        return httpx.Response(
            200,
            content=b"<html>please enable javascript</html>",
            request=httpx.Request("GET", _URL),
        )

    @pytest.mark.asyncio
    async def test_search_artist_raises_transport_error_on_malformed_json(self, client):
        client._http.request = AsyncMock(return_value=self._malformed_json_response())

        with pytest.raises(BandcampTransportError):
            await client.search_artist("Autechre", fail_fast=True)

    @pytest.mark.asyncio
    async def test_search_artist_default_mode_still_raises_value_error(self, client):
        # Default mode is untouched -- no new catch is added there.
        client._http.request = AsyncMock(return_value=self._malformed_json_response())

        with pytest.raises(ValueError):
            await client.search_artist("Autechre", fail_fast=False)

    @pytest.mark.asyncio
    async def test_search_albums_raises_transport_error_on_malformed_json(self, client):
        client._http.request = AsyncMock(return_value=self._malformed_json_response())

        with pytest.raises(BandcampTransportError):
            await client.search_albums("Autechre Confield", fail_fast=True)

    @pytest.mark.asyncio
    async def test_search_albums_default_mode_still_raises_value_error(self, client):
        client._http.request = AsyncMock(return_value=self._malformed_json_response())

        with pytest.raises(ValueError):
            await client.search_albums("Autechre Confield", fail_fast=False)

    @pytest.mark.asyncio
    async def test_malformed_json_records_transport_failure_not_aborted(self, client):
        # End-to-end through find_album_match's breaker wrapper: a malformed
        # body must be recorded exactly like any other transport failure
        # (record_transport_failure), not fall through to record_aborted --
        # otherwise a sustained Cloudflare-interstitial run would never trip
        # the breaker, same bug class as FIX 1.
        client._http.request = AsyncMock(return_value=self._malformed_json_response())
        breaker = get_bandcamp_probe_breaker()

        with (
            patch.object(
                breaker, "record_transport_failure", wraps=breaker.record_transport_failure
            ) as record_transport_failure,
            patch.object(breaker, "record_aborted", wraps=breaker.record_aborted) as record_aborted,
        ):
            with pytest.raises(BandcampTransportError):
                await client.find_album_match("Autechre", "Confield", fail_fast=True)

        record_transport_failure.assert_called_once_with(epoch=0)
        record_aborted.assert_not_called()


class TestFailFastMalshapedJsonBody:
    """LML#1106 review round 2, FIX B: ``search_artist`` / ``search_albums``
    guard ``resp.json()``'s PARSEABILITY (FIX 6, above) but not its SHAPE.
    A 200 whose body is valid JSON but not an object -- a bare list, a bare
    string -- or whose ``results`` field isn't a list still escapes: the
    ``for item in data.get("results", [])`` loop raises ``AttributeError``
    (``'list' object has no attribute 'get'`` / ``'str' object has no
    attribute 'get'``) or iterates over something that isn't item-shaped.
    That ``AttributeError`` escapes the fail-fast taxonomy entirely and
    lands in ``lookup/enrichment/bandcamp_probe.py``'s ``except Exception``
    catch-all -- a loud ``logger.exception`` per item (the #755 flood shape)
    routed through the no-op-in-CLOSED ``record_aborted`` instead of the
    counted ``record_transport_failure`` shed path, so the flood runs for
    the whole outage instead of tripping the breaker. Under ``fail_fast``,
    a mal-shaped-but-parseable body must raise :class:`BandcampTransportError`
    exactly like an unparseable one. Default mode is untouched -- these
    shapes still raise their raw ``AttributeError`` there, byte-identical to
    today (pinned by the two existing ``test_search_*_default_mode_still_
    raises_value_error`` tests above, which this class does not touch)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param([], id="bare_list"),
            pytest.param("a string", id="bare_string"),
            pytest.param({"results": "not-a-list"}, id="results_not_a_list"),
            pytest.param({"results": ["cloudflare"]}, id="results_items_are_strings"),
            pytest.param({"results": [None]}, id="results_items_are_null"),
            pytest.param({"results": [{"name": "ok"}, 123]}, id="results_items_mixed"),
        ],
    )
    async def test_search_artist_raises_transport_error_on_malshaped_body(self, client, body):
        client._http.request = AsyncMock(
            return_value=httpx.Response(200, json=body, request=httpx.Request("GET", _URL))
        )

        with pytest.raises(BandcampTransportError):
            await client.search_artist("Autechre", fail_fast=True)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param([], id="bare_list"),
            pytest.param("a string", id="bare_string"),
            pytest.param({"results": "not-a-list"}, id="results_not_a_list"),
            pytest.param({"results": ["cloudflare"]}, id="results_items_are_strings"),
            pytest.param({"results": [None]}, id="results_items_are_null"),
            pytest.param({"results": [{"name": "ok"}, 123]}, id="results_items_mixed"),
        ],
    )
    async def test_search_albums_raises_transport_error_on_malshaped_body(self, client, body):
        client._http.request = AsyncMock(
            return_value=httpx.Response(200, json=body, request=httpx.Request("GET", _URL))
        )

        with pytest.raises(BandcampTransportError):
            await client.search_albums("Autechre Confield", fail_fast=True)

    @pytest.mark.asyncio
    async def test_search_artist_default_mode_still_raises_attribute_error_on_bare_list(
        self, client
    ):
        # Default mode is untouched -- no new shape guard is added there;
        # a bare-list body still crashes with its raw AttributeError.
        client._http.request = AsyncMock(
            return_value=httpx.Response(200, json=[], request=httpx.Request("GET", _URL))
        )

        with pytest.raises(AttributeError):
            await client.search_artist("Autechre", fail_fast=False)

    @pytest.mark.asyncio
    async def test_search_albums_default_mode_still_raises_attribute_error_on_bare_list(
        self, client
    ):
        client._http.request = AsyncMock(
            return_value=httpx.Response(200, json=[], request=httpx.Request("GET", _URL))
        )

        with pytest.raises(AttributeError):
            await client.search_albums("Autechre Confield", fail_fast=False)

    @pytest.mark.asyncio
    async def test_malshaped_body_records_transport_failure_not_aborted(self, client):
        # End-to-end through find_album_match's breaker wrapper, mirroring
        # the unparseable-body pin above -- same taxonomy, same reason.
        client._http.request = AsyncMock(
            return_value=httpx.Response(200, json=[], request=httpx.Request("GET", _URL))
        )
        breaker = get_bandcamp_probe_breaker()

        with (
            patch.object(
                breaker, "record_transport_failure", wraps=breaker.record_transport_failure
            ) as record_transport_failure,
            patch.object(breaker, "record_aborted", wraps=breaker.record_aborted) as record_aborted,
        ):
            with pytest.raises(BandcampTransportError):
                await client.find_album_match("Autechre", "Confield", fail_fast=True)

        record_transport_failure.assert_called_once_with(epoch=0)
        record_aborted.assert_not_called()


class TestFindAlbumMatchFailFastBreakerIntegration:
    @pytest.mark.asyncio
    async def test_breaker_open_sheds_without_any_http_call(self, client):
        breaker = get_bandcamp_probe_breaker()
        breaker.force_open()
        client.search_artist = AsyncMock()

        with pytest.raises(BandcampBreakerOpenError):
            await client.find_album_match("Autechre", "Confield", fail_fast=True)

        client.search_artist.assert_not_called()

    @pytest.mark.asyncio
    async def test_breaker_open_error_carries_artist_album_context(self, client):
        # LML#1106 review FIX 7: a bare ``BandcampBreakerOpenError()`` groups
        # every shed under one message in Sentry -- give it the (artist,
        # title) context, mirroring discogs/admission.py:145.
        breaker = get_bandcamp_probe_breaker()
        breaker.force_open()

        with pytest.raises(BandcampBreakerOpenError, match="Autechre.*Confield"):
            await client.find_album_match("Autechre", "Confield", fail_fast=True)

    @pytest.mark.asyncio
    async def test_429_on_artist_search_propagates_and_records_shed(self, client):
        client._http.request = AsyncMock(return_value=_response(429))
        breaker = get_bandcamp_probe_breaker()

        with (
            patch.object(breaker, "record_shed", wraps=breaker.record_shed) as record_shed,
            patch.object(breaker, "record_success", wraps=breaker.record_success) as record_success,
            patch.object(breaker, "record_aborted", wraps=breaker.record_aborted) as record_aborted,
        ):
            with pytest.raises(BandcampRateLimitedError):
                await client.find_album_match("Autechre", "Confield", fail_fast=True)

        # Exactly one shed was recorded, and neither of the other two
        # terminal outcomes -- discriminates a mutation that rewires this
        # branch to any other ``record_*`` call (LML#1106 review, FIX 3).
        record_shed.assert_called_once_with(epoch=0)
        record_success.assert_not_called()
        record_aborted.assert_not_called()
        # One shed recorded; below the default threshold, so still CLOSED.
        assert breaker.state is BandcampBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_sustained_429s_trip_the_breaker_open(self, client):
        client._http.request = AsyncMock(return_value=_response(429))
        breaker = get_bandcamp_probe_breaker()

        for _ in range(3):
            with pytest.raises(BandcampRateLimitedError):
                await client.find_album_match("Autechre", "Confield", fail_fast=True)

        assert breaker.state is BandcampBreakerState.OPEN

    @pytest.mark.asyncio
    async def test_no_match_records_success_not_shed(self, client):
        # search_artist returns nothing, search_albums returns nothing ->
        # find_album_match returns None (genuine no-match), which must record
        # a breaker SUCCESS, not a shed.
        client._http.request = AsyncMock(
            side_effect=[
                httpx.Response(200, json={"results": []}, request=httpx.Request("GET", _URL)),
                httpx.Response(200, json={"results": []}, request=httpx.Request("GET", _URL)),
            ]
        )
        breaker = get_bandcamp_probe_breaker()

        with (
            patch.object(breaker, "record_success", wraps=breaker.record_success) as record_success,
            patch.object(breaker, "record_shed", wraps=breaker.record_shed) as record_shed,
            patch.object(breaker, "record_aborted", wraps=breaker.record_aborted) as record_aborted,
        ):
            result = await client.find_album_match(
                "Obscure Artist", "Obscure Album", fail_fast=True
            )

        assert result is None
        # Discriminates a mutation that rewires this branch to record_shed
        # (or any other outcome) -- LML#1106 review, FIX 3.
        record_success.assert_called_once_with(epoch=0)
        record_shed.assert_not_called()
        record_aborted.assert_not_called()
        assert breaker.state is BandcampBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_transport_failure_on_artist_search_does_not_record_success(self, client):
        # LML#1106 review FIX 2: a non-429 failure (5xx here) must not be
        # swallowed to a clean "no match" that records a breaker success --
        # that would let a HALF_OPEN trial falsely "recover" while Bandcamp
        # is still down, and would let resolve_streaming_url_with_cache
        # UPSERT a false-negative row.
        client._http.request = AsyncMock(return_value=_response(500))
        breaker = get_bandcamp_probe_breaker()

        with (
            patch.object(breaker, "record_success", wraps=breaker.record_success) as record_success,
            patch.object(breaker, "record_aborted", wraps=breaker.record_aborted) as record_aborted,
            patch.object(
                breaker, "record_transport_failure", wraps=breaker.record_transport_failure
            ) as record_transport_failure,
        ):
            with pytest.raises(BandcampTransportError):
                await client.find_album_match("Obscure Artist", "Obscure Album", fail_fast=True)

        record_success.assert_not_called()
        # LML#1106 review FIX 1: a transport failure is its own terminal
        # outcome now -- it must NOT fall through to record_aborted (which
        # is a no-op in CLOSED and would leave a sustained transport-failure
        # run undetected forever).
        record_aborted.assert_not_called()
        record_transport_failure.assert_called_once_with(epoch=0)
        assert breaker.state is BandcampBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_connect_error_on_artist_search_does_not_record_success(self, client):
        # Same shape as the 500 case above, but a network-layer failure
        # (connect timeout) rather than an HTTP error response.
        client._http.request = AsyncMock(side_effect=httpx.ConnectError("boom"))
        breaker = get_bandcamp_probe_breaker()

        with patch.object(
            breaker, "record_success", wraps=breaker.record_success
        ) as record_success:
            with pytest.raises(BandcampTransportError):
                await client.find_album_match("Obscure Artist", "Obscure Album", fail_fast=True)

        record_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_sustained_transport_failures_trip_the_breaker_open(self, client):
        # LML#1106 review FIX 1: repeated non-429 transport failures (a
        # sustained Cloudflare 403/1015 block, or a tarpit) must open the
        # breaker on their own -- before this fix nothing counted them, and
        # every fail-fast call kept hitting the network up to the full
        # 3-request ceiling forever.
        client._http.request = AsyncMock(return_value=_response(500))
        breaker = get_bandcamp_probe_breaker()

        for _ in range(3):
            with pytest.raises(BandcampTransportError):
                await client.find_album_match("Obscure Artist", "Obscure Album", fail_fast=True)

        assert breaker.state is BandcampBreakerState.OPEN

    @pytest.mark.asyncio
    async def test_unexpected_exception_records_aborted(self, client):
        # LML#1106 review FIX 5: nothing previously pinned that an
        # unexpected raise (not a shed, not a transport error) actually
        # reaches record_aborted -- deleting that handling left every
        # existing test green.
        breaker = get_bandcamp_probe_breaker()
        client.search_artist = AsyncMock(side_effect=RuntimeError("boom"))

        with patch.object(
            breaker, "record_aborted", wraps=breaker.record_aborted
        ) as record_aborted:
            with pytest.raises(RuntimeError, match="boom"):
                await client.find_album_match("Autechre", "Confield", fail_fast=True)

        record_aborted.assert_called_once_with(epoch=0)

    @pytest.mark.asyncio
    async def test_cancellation_during_half_open_trial_reopens_not_strands(
        self, client, clock, monkeypatch
    ):
        # LML#1106 review FIX 1 regression pin: asyncio.CancelledError is a
        # BaseException, the DESIGNED timeout mechanism for this mode
        # (#1098), and previously escaped both `except` clauses untouched --
        # stranding the breaker HALF_OPEN (shedding everyone for the full
        # watchdog window) instead of reopening for a fresh cool-down. Uses
        # a breaker with an injected fake clock (not the process-global one,
        # which runs on real time) so the HALF_OPEN promotion is deterministic.
        breaker = BandcampProbeBreaker(failure_threshold=1, cooldown_seconds=1.0, now=clock)
        breaker.record_shed(epoch=breaker.allow_request())  # CLOSED -> OPEN
        clock.advance(2.0)  # past the 1.0s cool-down

        monkeypatch.setattr("clients.bandcamp.get_bandcamp_probe_breaker", lambda: breaker)
        client._find_album_match_impl = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await client.find_album_match("Autechre", "Confield", fail_fast=True)

        # The trial call was cancelled -- it must reopen (fresh cool-down),
        # not strand HALF_OPEN (which would shed every subsequent caller for
        # the full watchdog window, ~200s at the old, un-retuned multiplier).
        assert breaker.state is BandcampBreakerState.OPEN
