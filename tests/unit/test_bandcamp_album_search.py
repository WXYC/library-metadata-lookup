"""Unit tests for the LML#1069 album-first Bandcamp discovery seam in clients/bandcamp.py.

Covers ``fix_autocomplete_url``, ``clean_title_for_query``, ``normalize_bc_title``,
``search_albums``, ``find_album_match_via_search``, and ``verify_album_page`` --
the shared method the offline album-search drain (scripts/bandcamp_pipeline.py)
and the PR2 runtime probe fallback both call.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from clients.bandcamp import (
    BandcampClient,
    BandcampSearchUnavailableError,
    clean_title_for_query,
    fix_autocomplete_url,
    normalize_bc_title,
    parse_og_title,
)
from clients.streaming.matching import SCORE_MATCH_ACCEPTANCE_FLOOR, score_match

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "bandcamp"


def _autocomplete_response(results: list[dict]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"results": results},
        request=httpx.Request("GET", "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"),
    )


def _error_response(status: int, url: str) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("GET", url))


def _album_result(
    name: str = "Aluminum Tunes",
    band_name: str = "Stereolab",
    url: str = "https://stereolab.bandcamp.com/album/aluminum-tunes",
) -> dict:
    return {"type": "a", "name": name, "band_name": band_name, "url": url}


class TestFixAutocompleteUrl:
    def test_dedoubles_doubled_url(self):
        doubled = (
            "https://into-the-light.bandcamp.comhttps://into-the-light.bandcamp.com"
            "/album/the-rules-of-the-game-original-studio-recordings-1978-1996"
        )
        assert fix_autocomplete_url(doubled) == (
            "https://into-the-light.bandcamp.com"
            "/album/the-rules-of-the-game-original-studio-recordings-1978-1996"
        )

    def test_passes_through_clean_url_unchanged(self):
        clean = "https://stereolab.bandcamp.com/album/aluminum-tunes"
        assert fix_autocomplete_url(clean) == clean

    def test_passes_through_custom_domain_unchanged(self):
        custom = "https://music.sufjan.com/album/whatever"
        assert fix_autocomplete_url(custom) == custom

    def test_dedoubles_custom_domain(self):
        doubled = "https://music.sufjan.comhttps://music.sufjan.com/album/whatever"
        assert fix_autocomplete_url(doubled) == "https://music.sufjan.com/album/whatever"

    def test_garbage_input_passed_through(self):
        assert fix_autocomplete_url("not a url") == "not a url"

    def test_empty_string(self):
        assert fix_autocomplete_url("") == ""


class TestCleanTitleForQuery:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ('Slide 12"', "Slide"),
            ('PH*CK What You Heard 12"', "PH*CK What You Heard"),
            ('Dog Star Man 12"', "Dog Star Man"),
            ("Sophisticated Ellington (studio) [missing]", "Sophisticated Ellington"),
            # No junk present -- passed through unchanged.
            ("Aluminum Tunes", "Aluminum Tunes"),
            # A real year-range parenthetical is NOT format junk; clean_title_for_query
            # must leave it alone (only normalize_bc_title's recall-recovery path
            # strips it, and only as a secondary acceptance test).
            (
                "The Rules of the Game: Original Studio Recordings (1978-1996)",
                "The Rules of the Game: Original Studio Recordings (1978-1996)",
            ),
        ],
    )
    def test_strips_format_junk_for_query_only(self, raw, expected):
        assert clean_title_for_query(raw) == expected

    def test_empty_string(self):
        assert clean_title_for_query("") == ""


class TestNormalizeBcTitle:
    """Parameterized against the LML#1069 measurement's near-miss corpus.

    TRUE rows must clear the 80/80 floor via ``score_match`` after
    ``normalize_bc_title`` is applied to both sides. FALSE rows must stay
    rejected even after normalization (their rejection lives on the artist
    axis, which this function never touches).
    """

    @pytest.mark.parametrize(
        "library_title,matched_title",
        [
            # Pine Hill Haints: bracketed catalog tag.
            ("Ghost Dance", "Ghost Dance [KLP186]"),
            # Z'ev: year-range parenthetical + slash spacing.
            ("as/if/when", "As / If / When (1978-83)"),
            # The Dutchess & The Duke: slash spacing only.
            ("Sunset/Sunrise", "Sunset / Sunrise"),
        ],
    )
    def test_true_rows_recover_above_floor(self, library_title, matched_title):
        score = score_match(normalize_bc_title(library_title), normalize_bc_title(matched_title))
        assert score >= SCORE_MATCH_ACCEPTANCE_FLOOR

    @pytest.mark.parametrize(
        "library_title,matched_title",
        [
            # Rainbow tribute -- must stay rejected after every normalization.
            (
                "Richie Blackmore's Rainbow",
                "Ride The Rainbow - The Ultimate Tribute to Ritchie Blackmore’s Rainbow",
            ),
            # Leo Sayer DJ edit -- must stay rejected.
            ("Thunder in My Heart", "Meck, Leo Sayer - Thunder in My Heart Again (2 VERSIONS)"),
            # U-Roy noise -- must stay rejected.
            ("Now", "U don't Know EP"),
            # Mercury Program wrong artist -- catalog-tag strip must NOT flip it.
            ("The Mercury Program", "Mercury [Phase 1]"),
        ],
    )
    def test_false_rows_stay_rejected_on_title_axis_too(self, library_title, matched_title):
        # These rows are rejected overall by the artist axis (see
        # test_find_album_match_via_search's end-to-end FALSE-row coverage);
        # this pins that normalize_bc_title's title-only patterns don't
        # accidentally manufacture a title-axis clear either.
        score = score_match(normalize_bc_title(library_title), normalize_bc_title(matched_title))
        assert score < SCORE_MATCH_ACCEPTANCE_FLOOR

    def test_christoph_de_babalon_catalog_number_prefix_is_accepted_loss(self):
        """Leading catalog number ("044 (Hilf Dir Selbst!)") is left unrecovered.

        No conservative prefix-strip pattern was justified without risking
        legitimate numeric-leading titles (e.g. "2112") -- accepted loss per
        the LML#1069 plan rather than a recovered near-miss.
        """
        score = score_match(
            normalize_bc_title("Hilf dir Selbsts"),
            normalize_bc_title("044 (Hilf Dir Selbst!)"),
        )
        assert score < SCORE_MATCH_ACCEPTANCE_FLOOR

    def test_empty_string(self):
        assert normalize_bc_title("") == ""


class TestSearchAlbums:
    @pytest.mark.asyncio
    async def test_parses_golden_repro_fixture(self):
        data = json.loads((FIXTURES_DIR / "autocomplete_a_golden_repro.json").read_text())
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=httpx.Response(
                200,
                json=data,
                request=httpx.Request(
                    "GET", "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"
                ),
            )
        )
        client._http = mock_http

        results = await client.search_albums("George Theodorakis The Rules of the Game")

        # Only the single type=="a" row survives; the interleaved type=="t" rows drop.
        assert len(results) == 1
        assert results[0]["artist"] == "George Theodorakis"
        assert (
            results[0]["title"] == "The Rules Of The Game: Original Studio Recordings (1978-1996)"
        )
        assert results[0]["url"] == (
            "https://into-the-light.bandcamp.com"
            "/album/the-rules-of-the-game-original-studio-recordings-1978-1996"
        )

    @pytest.mark.asyncio
    async def test_filters_non_album_types(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=_autocomplete_response(
                [
                    {"type": "b", "name": "Stereolab", "url": "https://stereolab.bandcamp.com"},
                    {
                        "type": "t",
                        "name": "Brakhage",
                        "band_name": "Stereolab",
                        "url": "https://stereolab.bandcamp.com/track/brakhage",
                    },
                ]
            )
        )
        client._http = mock_http

        results = await client.search_albums("Stereolab")
        assert results == []

    @pytest.mark.asyncio
    async def test_dedoubles_url_in_results(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=_autocomplete_response(
                [
                    _album_result(
                        url=(
                            "https://stereolab.bandcamp.comhttps://stereolab.bandcamp.com"
                            "/album/aluminum-tunes"
                        )
                    )
                ]
            )
        )
        client._http = mock_http

        results = await client.search_albums("Stereolab Aluminum Tunes")
        assert results[0]["url"] == "https://stereolab.bandcamp.com/album/aluminum-tunes"

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self):
        # Mirrors fetch_artist_catalog's None/[] split (#661): a transient
        # fetch failure must be distinguishable from a genuine empty result
        # set so the offline drain doesn't durably mark it not_found.
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=_error_response(
                500, "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"
            )
        )
        client._http = mock_http

        results = await client.search_albums("Nobody")
        assert results is None

    @pytest.mark.asyncio
    async def test_returns_none_on_network_error(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        client._http = mock_http

        results = await client.search_albums("Unreachable")
        assert results is None

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_genuinely_no_results(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(return_value=_autocomplete_response([]))
        client._http = mock_http

        results = await client.search_albums("xyznonexistent")
        assert results == []


class TestParseOgTitle:
    def test_splits_title_and_artist(self):
        assert parse_og_title("Aluminum Tunes, by Stereolab") == ("Aluminum Tunes", "Stereolab")

    def test_title_containing_comma(self):
        assert parse_og_title("Back, Baby, by Jessica Pratt") == ("Back, Baby", "Jessica Pratt")

    def test_no_match_returns_none(self):
        assert parse_og_title("Not the expected shape") is None

    def test_decodes_html_entities(self):
        """Bandcamp HTML-escapes og:title content -- an apostrophe in a real
        artist/title renders as ``&#39;`` in the page source. Without
        decoding, score_match compares the literal escaped text and a
        genuinely correct match can drop 40+ points below the acceptance
        floor (LML#1069 album-search backlog: The UMC's, id 2674)."""
        assert parse_og_title("Fruits Of Nature, by The UMC&#39;s") == (
            "Fruits Of Nature",
            "The UMC's",
        )

    def test_decodes_ampersand_entity(self):
        assert parse_og_title("Bombscare EP, by Low &amp; Spring Heel Jack") == (
            "Bombscare EP",
            "Low & Spring Heel Jack",
        )


class TestVerifyAlbumPage:
    @pytest.mark.asyncio
    async def test_accepts_matching_og_title(self):
        html = '<meta property="og:title" content="Aluminum Tunes, by Stereolab">'
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=httpx.Response(
                200,
                text=html,
                request=httpx.Request("GET", "https://stereolab.bandcamp.com/album/aluminum-tunes"),
            )
        )
        client._http = mock_http

        ok = await client.verify_album_page(
            "https://stereolab.bandcamp.com/album/aluminum-tunes", "Stereolab", "Aluminum Tunes"
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_rejects_mismatched_og_title(self):
        html = '<meta property="og:title" content="Totally Different Album, by Someone Else">'
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=httpx.Response(
                200,
                text=html,
                request=httpx.Request("GET", "https://someone.bandcamp.com/album/whatever"),
            )
        )
        client._http = mock_http

        ok = await client.verify_album_page(
            "https://someone.bandcamp.com/album/whatever", "Stereolab", "Aluminum Tunes"
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_returns_false_on_fetch_failure(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=_error_response(404, "https://gone.bandcamp.com/album/whatever")
        )
        client._http = mock_http

        ok = await client.verify_album_page(
            "https://gone.bandcamp.com/album/whatever", "Stereolab", "Aluminum Tunes"
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_returns_false_when_og_title_missing(self):
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=httpx.Response(
                200,
                text="<html><body>no meta tag here</body></html>",
                request=httpx.Request("GET", "https://someone.bandcamp.com/album/whatever"),
            )
        )
        client._http = mock_http

        ok = await client.verify_album_page(
            "https://someone.bandcamp.com/album/whatever", "Stereolab", "Aluminum Tunes"
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_accepts_when_only_normalized_titles_match(self):
        """LML#1069 85-row verify_failed floor: find_album_match_via_search
        accepts a hit via its second (normalize_bc_title) pass -- e.g. a
        library title carrying a cataloger annotation the real release
        title never had ("Dreamy [full-length]" vs the page's "Dreamy") --
        but verify_album_page re-scored the RAW un-normalized title against
        the page title and rejected an otherwise-correct match. verify_album_page
        must fall back to the same normalize_bc_title comparison the search
        step used to accept the match.
        """
        html = '<meta property="og:title" content="Dreamy, by Beat Happening">'
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=httpx.Response(
                200,
                text=html,
                request=httpx.Request("GET", "https://beathappening.bandcamp.com/album/dreamy"),
            )
        )
        client._http = mock_http

        ok = await client.verify_album_page(
            "https://beathappening.bandcamp.com/album/dreamy",
            "Beat Happening",
            "Dreamy [full-length]",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_still_rejects_when_normalized_titles_also_mismatch(self):
        """The normalize_bc_title fallback must not widen the floor itself --
        a genuinely wrong page still fails both passes."""
        html = '<meta property="og:title" content="Totally Different Album, by Someone Else">'
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=httpx.Response(
                200,
                text=html,
                request=httpx.Request("GET", "https://someone.bandcamp.com/album/whatever"),
            )
        )
        client._http = mock_http

        ok = await client.verify_album_page(
            "https://someone.bandcamp.com/album/whatever",
            "Stereolab",
            "Aluminum Tunes [full-length]",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_accepts_match_with_html_escaped_apostrophe(self):
        """Regression for LML#1069's album-search backlog: a real Bandcamp
        og:title HTML-escapes apostrophes, e.g. ``The UMC&#39;s``. Comparing
        the un-decoded text against the library's "The UMC's" scores 57.1
        (apostrophe-only diff), well below the 80 floor, even though the
        page is the correct match."""
        html = '<meta property="og:title" content="Fruits Of Nature, by The UMC&#39;s">'
        client = BandcampClient()
        mock_http = AsyncMock(spec=httpx.AsyncClient)
        mock_http.request = AsyncMock(
            return_value=httpx.Response(
                200,
                text=html,
                request=httpx.Request(
                    "GET", "https://wildpitchrecords.bandcamp.com/album/fruits-of-nature"
                ),
            )
        )
        client._http = mock_http

        ok = await client.verify_album_page(
            "https://wildpitchrecords.bandcamp.com/album/fruits-of-nature",
            "The UMC's",
            "Fruits of Nature",
        )
        assert ok is True


class TestFindAlbumMatchViaSearch:
    """End-to-end coverage over BandcampClient.find_album_match_via_search --
    the shared discovery method both the offline album-search drain and the
    PR2 runtime probe fallback call. Builds the query via clean_title_for_query,
    then accepts the first of (raw, normalize_bc_title-normalized) passes that
    clears the 80/80 floor via find_best_source_match.
    """

    @pytest.mark.asyncio
    async def test_golden_repro_matches_on_raw_pass(self):
        client = BandcampClient()
        client.search_albums = AsyncMock(
            return_value=[
                {
                    "artist": "George Theodorakis",
                    "title": "The Rules Of The Game: Original Studio Recordings (1978-1996)",
                    "url": (
                        "https://into-the-light.bandcamp.com"
                        "/album/the-rules-of-the-game-original-studio-recordings-1978-1996"
                    ),
                }
            ]
        )

        match = await client.find_album_match_via_search(
            "George Theodorakis",
            "The Rules of the Game: Original Studio Recordings (1978-1996)",
        )

        assert match is not None
        assert match.url == (
            "https://into-the-light.bandcamp.com"
            "/album/the-rules-of-the-game-original-studio-recordings-1978-1996"
        )
        assert match.confidence >= SCORE_MATCH_ACCEPTANCE_FLOOR

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_results(self):
        client = BandcampClient()
        client.search_albums = AsyncMock(return_value=[])

        match = await client.find_album_match_via_search("Nobody", "Nothing")
        assert match is None

    @pytest.mark.asyncio
    async def test_raises_on_search_unavailable(self):
        # search_albums returning None means the autocomplete request failed
        # after retries -- distinct from a genuine no-match so the offline
        # drain can avoid durably marking a transient blip not_found (#661).
        client = BandcampClient()
        client.search_albums = AsyncMock(return_value=None)

        with pytest.raises(BandcampSearchUnavailableError):
            await client.find_album_match_via_search("Nobody", "Nothing")

    @pytest.mark.asyncio
    async def test_pine_hill_haints_recovers_via_normalized_pass(self):
        client = BandcampClient()
        client.search_albums = AsyncMock(
            return_value=[
                {
                    "artist": "The Pine Hill Haints",
                    "title": "Ghost Dance [KLP186]",
                    "url": "https://pinehillhaints.bandcamp.com/album/ghost-dance",
                }
            ]
        )

        match = await client.find_album_match_via_search("Pine Hill Haints", "Ghost Dance")

        assert match is not None
        assert match.url == "https://pinehillhaints.bandcamp.com/album/ghost-dance"

    @pytest.mark.asyncio
    async def test_zev_recovers_via_normalized_pass(self):
        client = BandcampClient()
        client.search_albums = AsyncMock(
            return_value=[
                {
                    "artist": "Z'ev",
                    "title": "As / If / When (1978-83)",
                    "url": "https://zev.bandcamp.com/album/as-if-when",
                }
            ]
        )

        match = await client.find_album_match_via_search("Z'ev", "as/if/when")
        assert match is not None

    @pytest.mark.asyncio
    async def test_dutchess_and_duke_recovers_via_normalized_pass(self):
        client = BandcampClient()
        client.search_albums = AsyncMock(
            return_value=[
                {
                    "artist": "The Dutchess & The Duke",
                    "title": "Sunset / Sunrise",
                    "url": "https://dutchessandduke.bandcamp.com/album/sunset-sunrise",
                }
            ]
        )

        match = await client.find_album_match_via_search(
            "The Dutchess & The Duke", "Sunset/Sunrise"
        )
        assert match is not None

    @pytest.mark.asyncio
    async def test_rainbow_tribute_stays_rejected(self):
        client = BandcampClient()
        client.search_albums = AsyncMock(
            return_value=[
                {
                    "artist": "Various Artists",
                    "title": (
                        "Ride The Rainbow - The Ultimate Tribute to Ritchie Blackmore’s Rainbow"
                    ),
                    "url": "https://various.bandcamp.com/album/ride-the-rainbow",
                }
            ]
        )

        match = await client.find_album_match_via_search("Rainbow", "Richie Blackmore's Rainbow")
        assert match is None

    @pytest.mark.asyncio
    async def test_leo_sayer_dj_edit_stays_rejected(self):
        client = BandcampClient()
        client.search_albums = AsyncMock(
            return_value=[
                {
                    "artist": "Dj Sonixx",
                    "title": "Meck, Leo Sayer - Thunder in My Heart Again (2 VERSIONS)",
                    "url": "https://djsonixx.bandcamp.com/album/thunder",
                }
            ]
        )

        match = await client.find_album_match_via_search("Leo Sayer", "Thunder in My Heart")
        assert match is None

    @pytest.mark.asyncio
    async def test_u_roy_noise_stays_rejected(self):
        client = BandcampClient()
        client.search_albums = AsyncMock(
            return_value=[
                {
                    "artist": "Ory J",
                    "title": "U don't Know EP",
                    "url": "https://oryj.bandcamp.com/album/u-dont-know",
                }
            ]
        )

        match = await client.find_album_match_via_search("U-Roy", "Now")
        assert match is None

    @pytest.mark.asyncio
    async def test_mercury_program_wrong_artist_stays_rejected(self):
        # Catalog-tag bracket strip must not flip this: the title axis might
        # clear after normalize_bc_title, but the artist axis (63.4) never
        # does, and is_acceptable_match requires both.
        client = BandcampClient()
        client.search_albums = AsyncMock(
            return_value=[
                {
                    "artist": "The Program Initiative",
                    "title": "Mercury [Phase 1]",
                    "url": "https://programinitiative.bandcamp.com/album/mercury",
                }
            ]
        )

        match = await client.find_album_match_via_search(
            "The Mercury Program", "The Mercury Program"
        )
        assert match is None
