"""Unit tests for clients/streaming/matching.py."""

import pytest

from clients.streaming.matching import (
    is_acceptable_match,
    normalize_album_title,
    normalize_artist_name,
    score_match,
    strip_discogs_suffix,
    strip_format_suffix,
    strip_the_prefix,
)


class TestStripFormatSuffix:
    @pytest.mark.parametrize(
        "title, expected",
        [
            pytest.param('Automanikk 12"', "Automanikk", id="trailing-12-inch"),
            pytest.param('Conspiracy 7"', "Conspiracy", id="trailing-7-inch"),
            pytest.param('Round & Round 10"', "Round & Round", id="trailing-10-inch"),
            pytest.param("Abbey Road x 2", "Abbey Road", id="multi-disc-x2"),
            pytest.param("Confield x 3", "Confield", id="multi-disc-x3"),
            pytest.param("Parallel Lines LP", "Parallel Lines", id="trailing-lp"),
            pytest.param("Edits EP", "Edits", id="trailing-ep"),
            pytest.param("Confield", "Confield", id="no-suffix"),
            pytest.param("Aluminum Tunes", "Aluminum Tunes", id="no-suffix-multi-word"),
            pytest.param("Moon Pix (Deluxe Edition)", "Moon Pix", id="parenthetical-deluxe"),
            pytest.param("Homogenic (Remastered)", "Homogenic", id="parenthetical-remastered"),
            pytest.param("DOGA (Reissue)", "DOGA", id="parenthetical-reissue"),
            pytest.param("Honeybear (Expanded Edition)", "Honeybear", id="parenthetical-expanded"),
            pytest.param('7" And 7 Is', '7" And 7 Is', id="mid-title-quote-not-stripped"),
            pytest.param("", "", id="empty-string"),
            pytest.param("S/t", "S/t", id="self-titled-unchanged"),
            pytest.param(
                "Hotel Insomnia (Limited Edition)", "Hotel Insomnia", id="parenthetical-limited"
            ),
            pytest.param(
                "Gasoline (Anniversary Edition)", "Gasoline", id="parenthetical-anniversary"
            ),
            pytest.param("Me Myself and I [single]", "Me Myself and I", id="bracket-single"),
            pytest.param("Coma [single]", "Coma", id="bracket-single-short"),
            pytest.param("Noises [EP]", "Noises", id="bracket-ep"),
            pytest.param("Laminations [sampler EP]", "Laminations", id="bracket-sampler-ep"),
            pytest.param("College Karma [EP]", "College Karma", id="bracket-ep-two-words"),
        ],
    )
    def test_strip_format_suffix(self, title, expected):
        assert strip_format_suffix(title) == expected


class TestNormalizeAlbumTitle:
    @pytest.mark.parametrize(
        "title, expected",
        [
            pytest.param("Aluminum Tunes", "aluminum tunes", id="simple-lowercase"),
            pytest.param('Automanikk 12"', "automanikk", id="strip-then-normalize"),
            pytest.param("Café Tacvba EP", "cafe tacvba", id="diacritics-and-suffix"),
            pytest.param("", "", id="empty-string"),
        ],
    )
    def test_normalize_album_title(self, title, expected):
        assert normalize_album_title(title) == expected


class TestNormalizeArtistName:
    @pytest.mark.parametrize(
        "artist, expected",
        [
            pytest.param("Stereolab", "stereolab", id="simple-lowercase"),
            pytest.param("Björk", "bjork", id="diacritics"),
            pytest.param("Hüsker Dü", "husker du", id="multiple-diacritics"),
            pytest.param("", "", id="empty-string"),
        ],
    )
    def test_normalize_artist_name(self, artist, expected):
        assert normalize_artist_name(artist) == expected


class TestScoreMatch:
    def test_exact_match(self):
        assert score_match("Stereolab", "Stereolab") == 100.0

    def test_case_insensitive_match(self):
        assert score_match("stereolab", "Stereolab") == 100.0

    def test_diacritics_match(self):
        assert score_match("Bjork", "Björk") == 100.0

    def test_remastered_suffix_high_score(self):
        assert score_match("Confield", "Confield (Remastered)") >= 80.0

    def test_completely_different_low_score(self):
        assert score_match("Stereolab", "Stereo MC's") < 80.0

    def test_empty_strings(self):
        assert score_match("", "") == 100.0

    def test_one_empty_string(self):
        assert score_match("Stereolab", "") == 0.0

    def test_the_prefix_mismatch(self):
        """'Afros' should score well against 'The Afros'."""
        assert score_match("Afros", "The Afros") >= 80.0


class TestIsAcceptableMatch:
    def test_both_high_accepted(self):
        assert is_acceptable_match(95.0, 90.0) is True

    def test_both_at_threshold_accepted(self):
        assert is_acceptable_match(80.0, 80.0) is True

    def test_artist_below_threshold_rejected(self):
        assert is_acceptable_match(75.0, 95.0) is False

    def test_title_below_threshold_rejected(self):
        assert is_acceptable_match(95.0, 60.0) is False

    def test_both_below_threshold_rejected(self):
        assert is_acceptable_match(50.0, 50.0) is False


class TestStripDiscogsSuffix:
    @pytest.mark.parametrize(
        "name, expected",
        [
            pytest.param("DNA (22)", "DNA", id="numbered-suffix"),
            pytest.param("Asia (2)", "Asia", id="single-digit"),
            pytest.param("Björk", "Björk", id="no-suffix"),
            pytest.param("The Afros", "The Afros", id="no-suffix-the"),
            pytest.param("", "", id="empty"),
            pytest.param(
                "Moon Pix (Deluxe Edition)",
                "Moon Pix (Deluxe Edition)",
                id="non-numeric-parens-preserved",
            ),
        ],
    )
    def test_strip_discogs_suffix(self, name, expected):
        assert strip_discogs_suffix(name) == expected


class TestFindBestMatch:
    """Tests for the generic find_best_match function."""

    def test_finds_best_by_combined_score(self):
        from clients.streaming.matching import find_best_match

        results = [
            {"artist": "Stereolab", "album": "Dots and Loops", "url": "http://a"},
            {"artist": "Stereolab", "album": "Aluminum Tunes", "url": "http://b"},
        ]
        best = find_best_match(
            results,
            "Stereolab",
            "Aluminum Tunes",
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: r["album"],
            url_fn=lambda r: r["url"],
        )
        assert best is not None
        assert best["url"] == "http://b"
        assert best["matched_title"] == "Aluminum Tunes"

    def test_returns_none_when_no_acceptable_match(self):
        from clients.streaming.matching import find_best_match

        results = [
            {"artist": "Cat Power", "album": "Moon Pix", "url": "http://x"},
        ]
        best = find_best_match(
            results,
            "Autechre",
            "Confield",
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: r["album"],
            url_fn=lambda r: r["url"],
        )
        assert best is None

    def test_returns_none_for_empty_results(self):
        from clients.streaming.matching import find_best_match

        best = find_best_match(
            [],
            "Stereolab",
            "Aluminum Tunes",
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: r["album"],
            url_fn=lambda r: r["url"],
        )
        assert best is None

    def test_includes_id_when_id_fn_provided(self):
        from clients.streaming.matching import find_best_match

        results = [
            {"artist": "Stereolab", "album": "Aluminum Tunes", "url": "http://b", "id": "abc123"},
        ]
        best = find_best_match(
            results,
            "Stereolab",
            "Aluminum Tunes",
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: r["album"],
            url_fn=lambda r: r["url"],
            id_fn=lambda r: r["id"],
        )
        assert best is not None
        assert best["id"] == "abc123"

    def test_no_id_key_when_id_fn_omitted(self):
        from clients.streaming.matching import find_best_match

        results = [
            {"artist": "Stereolab", "album": "Aluminum Tunes", "url": "http://b"},
        ]
        best = find_best_match(
            results,
            "Stereolab",
            "Aluminum Tunes",
            artist_fn=lambda r: r["artist"],
            title_fn=lambda r: r["album"],
            url_fn=lambda r: r["url"],
        )
        assert best is not None
        assert "id" not in best

    def test_spotify_field_extraction(self):
        """Verifies find_best_match works with Spotify's nested response format."""
        from clients.streaming.matching import find_best_match

        results = [
            {
                "name": "Aluminum Tunes",
                "artists": [{"name": "Stereolab"}],
                "external_urls": {"spotify": "https://open.spotify.com/album/xyz"},
                "id": "xyz",
            },
        ]
        best = find_best_match(
            results,
            "Stereolab",
            "Aluminum Tunes",
            artist_fn=lambda r: r.get("artists", [{}])[0].get("name", ""),
            title_fn=lambda r: r.get("name", ""),
            url_fn=lambda r: r.get("external_urls", {}).get("spotify", ""),
            id_fn=lambda r: r.get("id", ""),
        )
        assert best is not None
        assert best["url"] == "https://open.spotify.com/album/xyz"
        assert best["id"] == "xyz"
        assert best["confidence"] > 0


class TestFindBestTypedMatch:
    """Tests for find_best_typed_match — returns the original typed object."""

    def test_returns_highest_combined_score_original(self):
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        slightly_off = make_discogs_result(
            release_id=1, album="Aluminium Tunes", artist="Stereolab"
        )
        exact = make_discogs_result(release_id=2, album="Aluminum Tunes", artist="Stereolab")
        best = find_best_typed_match(
            [slightly_off, exact],
            query_artist="Stereolab",
            query_title="Aluminum Tunes",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is exact

    def test_returns_none_when_no_candidate_clears_floor(self):
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        candidates = [
            make_discogs_result(release_id=1, album="Moon Pix", artist="Cat Power"),
            make_discogs_result(release_id=2, album="Greatest Hits", artist="Queen"),
        ]
        best = find_best_typed_match(
            candidates,
            query_artist="Autechre",
            query_title="Confield",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is None

    def test_returns_none_for_empty_iterable(self):
        from clients.streaming.matching import find_best_typed_match

        best = find_best_typed_match(
            [],
            query_artist="Stereolab",
            query_title="Aluminum Tunes",
            artist_fn=lambda r: "",
            title_fn=lambda r: "",
        )
        assert best is None

    def test_handles_none_fields_on_candidate(self):
        """A candidate whose artist or title extractor returns None scores as 0
        and is rejected — not raised."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        # DiscogsSearchResult.artist defaults to None; verify it doesn't blow up.
        broken = make_discogs_result(release_id=1, album="Aluminum Tunes", artist=None)
        good = make_discogs_result(release_id=2, album="Aluminum Tunes", artist="Stereolab")
        best = find_best_typed_match(
            [broken, good],
            query_artist="Stereolab",
            query_title="Aluminum Tunes",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is good

    def test_first_result_wins_on_tie(self):
        """Ties resolve to the first candidate reaching the score — mirrors
        find_best_match's order-preserving behavior."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        first = make_discogs_result(release_id=1, album="Aluminum Tunes", artist="Stereolab")
        second = make_discogs_result(release_id=2, album="Aluminum Tunes", artist="Stereolab")
        best = find_best_typed_match(
            [first, second],
            query_artist="Stereolab",
            query_title="Aluminum Tunes",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is first

    def test_query_artist_accepts_variant_list_and_takes_max(self):
        """When the search-query form ('Various') differs from the canonical
        form Discogs returns ('Various Artists'), passing both variants lets
        the score clear the floor — score_match('Various', 'Various Artists')
        is 63.6, but score_match('Various Artists', 'Various Artists') is
        100, and the max wins."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        candidate = make_discogs_result(
            release_id=42, album="Disco Not Disco", artist="Various Artists"
        )
        best = find_best_typed_match(
            [candidate],
            query_artist=["Various", "Various Artists"],
            query_title="Disco Not Disco",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is candidate

    def test_query_title_accepts_variant_list_and_takes_max(self):
        """When the search-query form (a long canonical title) differs from
        the form the candidate carries (short library title), passing both
        variants lets the score clear the floor."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        candidate = make_discogs_result(release_id=42, album="Disco Not Disco", artist="Various")
        best = find_best_typed_match(
            [candidate],
            query_artist="Various",
            query_title=[
                "Disco Not Disco (Post Punk, Electro & Leftfield Disco Classics)",
                "Disco Not Disco",
            ],
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is candidate

    def test_returns_none_when_query_artist_is_empty_string(self):
        """score_match('','') returns 100 by rapidfuzz convention. Without a
        guard, a candidate with r.artist=None (coerced to '') and r.album=''
        would trivially clear 100/100 against an empty query. Defense against
        sparse library rows + malformed Discogs candidates."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        # Both candidate fields parse as empty/None.
        junk = make_discogs_result(release_id=99, album="", artist=None)
        best = find_best_typed_match(
            [junk],
            query_artist="",
            query_title="Aluminum Tunes",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is None

    def test_returns_none_when_query_title_is_empty_string(self):
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        junk = make_discogs_result(release_id=99, album=None, artist="")
        best = find_best_typed_match(
            [junk],
            query_artist="Stereolab",
            query_title="",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is None

    def test_returns_none_when_every_variant_is_empty(self):
        """Variant lists with only empty strings are equivalent to an empty
        query and short-circuit to no-match."""
        from clients.streaming.matching import find_best_typed_match
        from tests.factories import make_discogs_result

        junk = make_discogs_result(release_id=99, album="", artist="")
        best = find_best_typed_match(
            [junk],
            query_artist=["", ""],
            query_title="Foo",
            artist_fn=lambda r: r.artist,
            title_fn=lambda r: r.album,
        )
        assert best is None


class TestStripThePrefix:
    @pytest.mark.parametrize(
        "name, expected",
        [
            pytest.param("The Afros", "Afros", id="strips-the"),
            pytest.param("Stereolab", "Stereolab", id="no-the"),
            pytest.param("The The", "The", id="the-the"),
            pytest.param("Theodore", "Theodore", id="the-substring-preserved"),
            pytest.param("", "", id="empty"),
        ],
    )
    def test_strip_the_prefix(self, name, expected):
        assert strip_the_prefix(name) == expected
