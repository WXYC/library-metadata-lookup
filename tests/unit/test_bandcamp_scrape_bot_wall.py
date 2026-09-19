"""Unit tests for LML#1326: a bot-wall interstitial on Bandcamp's HTML-scraping
legs must be classified "couldn't ask", never "genuinely empty catalog".

``fetch_artist_catalog`` fetches ``{slug}.bandcamp.com/music`` and scrapes
``resp.text`` with the ``_ALBUM_*_RE`` regexes, so it has no unparseable-*body*
case in LML#1323's sense -- there is no ``resp.json()`` to fail. What it had
instead was worse: a Bandcamp/Cloudflare interstitial at HTTP 200 matches no
album regex and returns ``[]`` in BOTH modes, which is the one value this
method's contract declares to mean "the artist has no releases". Two durable
consequences, both confirmed against the LML#1323 branch:

- ``scripts/bandcamp_pipeline.py::phase_lookup`` routes that ``[]`` to its
  "genuinely empty catalog: definitively absent" branch and
  ``mark_bandcamp_not_found``s every album for the slug.
- the live path gets ``catalog_leg_failed=False``, so a clean album-first
  fallback miss returns ``None`` -> a 7-day known-miss row plus
  ``on_streaming=False`` written through to Backend.

The detection keys on POSITIVE evidence that the response is an interstitial
rather than a Bandcamp page -- never on "no albums were found". Keying off
emptiness would reclassify every genuinely empty catalog as a failure, the same
bug in the opposite direction. ``TestDetectBotWallNegatives`` is the half that
pins that: the fixtures there include a real empty-catalog page, the
``noscript`` "please enable javascript" copy a real Bandcamp page carries, and
Cloudflare's JS-Detections script tag, which is injected into *legitimate* 200
responses and is therefore deliberately NOT a marker.

Each caller asks the question only on the branch where it could not read the
page it wanted -- ``fetch_artist_catalog`` when the scrape found no albums,
``verify_album_page`` when there is no ``og:title`` to score. That ordering
decides nothing (``detect_bot_wall`` returns the same verdict either way); what
it buys is collision-proofing for the one shape that could otherwise bite,
since a Bandcamp document ``<title>`` carries the band's or the album's own
name. ``test_a_band_whose_name_collides_with_a_challenge_phrase_still_scrapes``
and ``test_an_album_named_like_a_challenge_page_still_verifies`` pin that, and
``test_that_band_with_an_empty_catalog_is_the_accepted_cost`` pins the residue
it does not close.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from clients.bandcamp import (
    BandcampClient,
    BandcampTransportError,
    detect_bot_wall,
)

_MUSIC_URL = "https://autechre.bandcamp.com/music"
_AUTOCOMPLETE_URL = "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"


class _InstantLimiter:
    """A rate limiter stand-in with a no-op ``acquire`` -- see the identical
    fixture in ``test_bandcamp_client.py`` / ``test_bandcamp_fail_fast_probe.py``.
    Duplicated per this repo's convention for private test helpers."""

    async def acquire(self) -> None:
        return None


# --- wall shapes (positive evidence) ----------------------------------------

# Cloudflare's managed-challenge interstitial: served at 200 with a
# ``Just a moment...`` document title, the challenge form, and the
# ``_cf_chl_opt`` bootstrap object.
CF_MANAGED_CHALLENGE_HTML = """<!DOCTYPE html><html lang="en-US"><head>
<title>Just a moment...</title>
<meta http-equiv="X-UA-Compatible" content="IE=Edge">
</head><body class="no-js">
<div class="main-wrapper" role="main">
<form id="challenge-form" action="/music?__cf_chl_f_tk=abc" method="POST"></form>
<script>window._cf_chl_opt={cvId:'3',cType:'managed',cRay:'8ab'};</script>
</div></body></html>"""

# Cloudflare's WAF block page (rule 1020) -- also seen at 200 behind some
# configurations, which is why the non-200 raise above does not cover it.
CF_WAF_BLOCK_HTML = """<!DOCTYPE html><html><head>
<title>Attention Required! | Cloudflare</title>
</head><body><h1>Sorry, you have been blocked</h1>
<p>You are unable to access bandcamp.com</p>
<div class="cf-error-details">Cloudflare Ray ID: 8abc</div>
</body></html>"""

# A wall whose document title says nothing: only the challenge bootstrap
# object gives it away.
CF_CHALLENGE_BODY_ONLY_HTML = (
    "<html><head><title>bandcamp.com</title></head><body>"
    "<script>window._cf_chl_opt={cvId:'3'};</script></body></html>"
)

# --- genuine page shapes (must never be read as a wall) ----------------------

# A real, Bandcamp-served ``/music`` page for an artist with no releases. Note
# the two traps it carries on purpose:
#   * ``please enable javascript`` -- real Bandcamp pages say this in a
#     ``noscript`` block (the player needs JS). It is the body of LML#1323's
#     autocomplete bot-wall FIXTURE, where it was harmless because the JSON
#     parse was the thing that failed; as a scrape-leg marker it would fire on
#     every real page.
#   * ``/cdn-cgi/challenge-platform/scripts/jsd/main.js`` -- Cloudflare's JS
#     Detections injects this script into LEGITIMATE 200 responses, so the
#     ``challenge-platform`` path is evidence that a site is behind Cloudflare,
#     not evidence that this response is a challenge.
GENUINE_EMPTY_CATALOG_HTML = """<!DOCTYPE html><html><head>
<title>Music | Csillagrablók</title>
<meta property="og:site_name" content="Bandcamp">
<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>
</head><body>
<noscript>Please enable javascript to use the Bandcamp player.</noscript>
<div id="discography"></div>
</body></html>"""

# A real page whose ALBUM is called "Just a Moment" -- the title-marker scan is
# scoped to the document ``<title>`` element precisely so album content can
# never trip it.
GENUINE_CATALOG_WITH_AWKWARD_ALBUM_TITLE_HTML = """<!DOCTYPE html><html><head>
<title>Music | Jessica Pratt</title>
</head><body>
<li><a href="/album/just-a-moment"><p class="title">Just a Moment</p></a></li>
</body></html>"""


def _scrape_response(
    html: str,
    *,
    url: str = _MUSIC_URL,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        200,
        content=html.encode("utf-8"),
        headers=headers,
        request=httpx.Request("GET", url),
    )


def _client() -> BandcampClient:
    client = BandcampClient()
    client._http = AsyncMock(spec=httpx.AsyncClient)
    client._rate_limiter = _InstantLimiter()  # type: ignore[assignment]
    return client


class TestDetectBotWallPositives:
    """Every signal is structural (a document title, a challenge-bootstrap
    token, a Cloudflare mitigation header, or a body with no document in it at
    all) so that none of them can be produced by an artist's own page
    content."""

    @pytest.mark.parametrize(
        "html,expected_fragment",
        [
            (CF_MANAGED_CHALLENGE_HTML, "just a moment"),
            (CF_WAF_BLOCK_HTML, "attention required"),
            (CF_CHALLENGE_BODY_ONLY_HTML, "_cf_chl_opt"),
            (
                '<html><head><title>x</title></head><body><form id="challenge-form">'
                "</form></body></html>",
                "challenge-form",
            ),
            ("", "empty body"),
            ("   \n\t ", "empty body"),
        ],
    )
    def test_names_the_signal_it_matched(self, html, expected_fragment):
        signal = detect_bot_wall(html)
        assert signal is not None
        assert expected_fragment in signal

    def test_cf_mitigated_header_alone_is_enough(self):
        # Cloudflare stamps ``cf-mitigated`` on a challenged response. It can
        # never appear on a page Bandcamp actually served us, so it needs no
        # corroboration from the body.
        signal = detect_bot_wall(GENUINE_EMPTY_CATALOG_HTML, {"cf-mitigated": "challenge"})
        assert signal is not None
        assert "cf-mitigated" in signal


class TestDetectBotWallNegatives:
    """The other half of the design constraint: a page that merely has no
    albums on it is not a wall."""

    @pytest.mark.parametrize(
        "html",
        [
            GENUINE_EMPTY_CATALOG_HTML,
            GENUINE_CATALOG_WITH_AWKWARD_ALBUM_TITLE_HTML,
            # The fixture shapes the pre-existing suites use: bare fragments
            # with no ``<head>`` at all must stay classified as pages.
            "<html><body>no releases here</body></html>",
            '<a href="/album/confield"><p class="title">Confield</p></a>',
            '<meta property="og:title" content="Aluminum Tunes, by Stereolab">',
        ],
    )
    def test_returns_none(self, html):
        assert detect_bot_wall(html) is None


class TestFetchArtistCatalogBotWall:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("fail_fast", [False, True])
    @pytest.mark.parametrize(
        "html,headers",
        [
            (CF_MANAGED_CHALLENGE_HTML, None),
            (CF_WAF_BLOCK_HTML, None),
            (CF_CHALLENGE_BODY_ONLY_HTML, None),
            (GENUINE_EMPTY_CATALOG_HTML, {"cf-mitigated": "challenge"}),
        ],
    )
    async def test_interstitial_is_couldnt_ask_in_both_modes(self, fail_fast, html, headers):
        # The whole of LML#1326: before the fix this returned ``[]`` in both
        # modes -- the value the contract reserves for "the artist has no
        # releases" -- so a block was durably recorded as a definitive absence.
        client = _client()
        client._http.request = AsyncMock(return_value=_scrape_response(html, headers=headers))

        with pytest.raises(BandcampTransportError):
            await client.fetch_artist_catalog("autechre", fail_fast=fail_fast)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fail_fast", [False, True])
    async def test_genuine_empty_catalog_still_returns_the_empty_shape(self, fail_fast):
        # The direction the fix must NOT break: a Bandcamp-served page with no
        # album links is still a definitive empty catalog, and the pipeline is
        # still free to record it.
        client = _client()
        client._http.request = AsyncMock(return_value=_scrape_response(GENUINE_EMPTY_CATALOG_HTML))

        assert await client.fetch_artist_catalog("csillagrablok", fail_fast=fail_fast) == []

    @pytest.mark.asyncio
    async def test_album_named_like_a_challenge_page_still_scrapes(self):
        client = _client()
        client._http.request = AsyncMock(
            return_value=_scrape_response(
                GENUINE_CATALOG_WITH_AWKWARD_ALBUM_TITLE_HTML,
                url="https://jessicapratt.bandcamp.com/music",
            )
        )

        albums = await client.fetch_artist_catalog("jessicapratt")

        assert albums == [
            {
                "url": "https://jessicapratt.bandcamp.com/album/just-a-moment",
                "title": "Just a Moment",
            }
        ]

    @pytest.mark.asyncio
    async def test_a_band_whose_name_collides_with_a_challenge_phrase_still_scrapes(self):
        # The collision the call-site ordering closes: a ``/music`` page's
        # document title is "Music | <Band>", so a band called "Access Denied"
        # carries a challenge-page phrase in its own title. The wall question is
        # only asked when the scrape found nothing, so their catalog parses
        # normally.
        client = _client()
        client._http.request = AsyncMock(
            return_value=_scrape_response(
                "<!DOCTYPE html><html><head><title>Music | Access Denied</title></head>"
                '<body><li><a href="/album/first-lp"><p class="title">First LP</p></a></li>'
                "</body></html>",
                url="https://accessdenied.bandcamp.com/music",
            )
        )

        albums = await client.fetch_artist_catalog("accessdenied")

        assert albums == [
            {"url": "https://accessdenied.bandcamp.com/album/first-lp", "title": "First LP"}
        ]

    @pytest.mark.asyncio
    async def test_that_band_with_an_empty_catalog_is_the_accepted_cost(self):
        # The residue of the same collision, pinned rather than hidden: the same
        # band with NO releases does read as a wall, so their slug stays
        # ``pending`` instead of being marked absent. That is the safe direction
        # (no durable negative is written, and a re-run is free), and it is the
        # price of not calibrating on page chrome. If it ever bites a real
        # artist, narrow ``_BOT_WALL_TITLE_MARKERS`` -- do not widen the
        # emptiness gate into the evidence.
        client = _client()
        client._http.request = AsyncMock(
            return_value=_scrape_response(
                "<!DOCTYPE html><html><head><title>Music | Access Denied</title></head>"
                '<body><div id="discography"></div></body></html>',
                url="https://accessdenied.bandcamp.com/music",
            )
        )

        with pytest.raises(BandcampTransportError):
            await client.fetch_artist_catalog("accessdenied")

    @pytest.mark.asyncio
    async def test_raise_is_tellable_from_a_plain_transport_failure(self):
        # Same argument as LML#1323's ``unparseable body`` marker: a block that
        # persists must be diagnosable at every log site above, so the message
        # names the shape AND the signal that identified it.
        client = _client()
        client._http.request = AsyncMock(return_value=_scrape_response(CF_MANAGED_CHALLENGE_HTML))

        with pytest.raises(BandcampTransportError) as wall:
            await client.fetch_artist_catalog("autechre")

        assert "bot-wall" in str(wall.value)
        assert "just a moment" in str(wall.value)

        client._http.request = AsyncMock(
            return_value=httpx.Response(500, request=httpx.Request("GET", _MUSIC_URL))
        )
        with pytest.raises(BandcampTransportError) as server_error:
            await client.fetch_artist_catalog("autechre")
        assert "bot-wall" not in str(server_error.value)


class TestLivePathLeavesCatalogLegFailed:
    """The live half of the harm: ``_find_album_match_impl`` must see the wall
    as a catalog-leg failure, so a clean album-first fallback miss raises (the
    LML#1106 round-2 FIX A branch) instead of resolving to a cachable ``None``
    -- which is what writes the 7-day known-miss row and ``on_streaming=False``
    through to Backend."""

    @pytest.mark.asyncio
    async def test_wall_plus_clean_fallback_miss_raises_rather_than_returning_none(self):
        client = _client()
        client._http.request = AsyncMock(
            side_effect=[
                # 1: search_artist -> a matching band.
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
                    request=httpx.Request("GET", _AUTOCOMPLETE_URL),
                ),
                # 2: fetch_artist_catalog -> the bot wall.
                _scrape_response(CF_MANAGED_CHALLENGE_HTML),
                # 3: search_albums (album-first fallback) -> a clean empty index.
                httpx.Response(
                    200, json={"results": []}, request=httpx.Request("GET", _AUTOCOMPLETE_URL)
                ),
            ]
        )

        with pytest.raises(BandcampTransportError):
            await client.find_album_match("Autechre", "Confield")

        # All three legs ran: the wall on {slug}.bandcamp.com must not
        # pre-empt the sibling call to bandcamp.com/api/fuzzysearch (the
        # different-hosts argument of LML#1326).
        assert client._http.request.await_count == 3

    @pytest.mark.asyncio
    async def test_genuine_empty_catalog_plus_clean_fallback_miss_still_returns_none(self):
        # The control: with a real empty catalog the same three legs resolve to
        # a negative-cacheable ``None``, unchanged. Only the wall moved.
        client = _client()
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
                    request=httpx.Request("GET", _AUTOCOMPLETE_URL),
                ),
                _scrape_response(GENUINE_EMPTY_CATALOG_HTML),
                httpx.Response(
                    200, json={"results": []}, request=httpx.Request("GET", _AUTOCOMPLETE_URL)
                ),
            ]
        )

        assert await client.find_album_match("Autechre", "Confield") is None


class TestVerifyAlbumPageBotWall:
    """``verify_album_page`` shares the scrape shape but NOT the harm, so
    LML#1326 deliberately leaves its ``False`` contract alone and only makes the
    wall tellable. Its only caller is ``phase_album_search``'s ``verify_hits``
    leg, where a ``False`` tallies ``verify_failed`` and writes NOTHING -- the
    row stays ``pending``/re-runnable. Raising instead would land in that
    caller's ``except Exception`` and produce the same tally with a traceback
    per row, so it would buy no data safety and cost log volume. The reason is
    recorded in the method's docstring; these tests pin both halves."""

    @pytest.mark.asyncio
    async def test_returns_false_and_names_the_wall_in_the_log(self, caplog):
        client = _client()
        client._http.request = AsyncMock(
            return_value=_scrape_response(
                CF_MANAGED_CHALLENGE_HTML,
                url="https://stereolab.bandcamp.com/album/aluminum-tunes",
            )
        )

        with caplog.at_level(logging.WARNING, logger="clients.bandcamp"):
            ok = await client.verify_album_page(
                "https://stereolab.bandcamp.com/album/aluminum-tunes",
                "Stereolab",
                "Aluminum Tunes",
            )

        assert ok is False
        assert "bot-wall" in caplog.text
        assert "just a moment" in caplog.text

    @pytest.mark.asyncio
    async def test_an_album_named_like_a_challenge_page_still_verifies(self, caplog):
        # An album page's document title carries the ALBUM's name, which is why
        # the wall question is asked only when there is no ``og:title`` to score.
        client = _client()
        client._http.request = AsyncMock(
            return_value=_scrape_response(
                "<!DOCTYPE html><html><head><title>Just a Moment | Jessica Pratt</title>"
                '<meta property="og:title" content="Just a Moment, by Jessica Pratt">'
                "</head><body></body></html>",
                url="https://jessicapratt.bandcamp.com/album/just-a-moment",
            )
        )

        with caplog.at_level(logging.WARNING, logger="clients.bandcamp"):
            ok = await client.verify_album_page(
                "https://jessicapratt.bandcamp.com/album/just-a-moment",
                "Jessica Pratt",
                "Just a Moment",
            )

        assert ok is True
        assert "bot-wall" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_genuine_mismatch_is_not_logged_as_a_wall(self, caplog):
        client = _client()
        client._http.request = AsyncMock(
            return_value=_scrape_response(
                "<html><head><title>Something Else | Someone</title></head><body>"
                '<meta property="og:title" content="Something Else, by Someone Else">'
                "</body></html>",
                url="https://someone.bandcamp.com/album/something-else",
            )
        )

        with caplog.at_level(logging.WARNING, logger="clients.bandcamp"):
            ok = await client.verify_album_page(
                "https://someone.bandcamp.com/album/something-else",
                "Stereolab",
                "Aluminum Tunes",
            )

        assert ok is False
        assert "bot-wall" not in caplog.text
