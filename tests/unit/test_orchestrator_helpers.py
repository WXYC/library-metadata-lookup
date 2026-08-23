"""Unit tests for orchestrator helper functions.

These test the individual functions extracted from routers/request.py:
- resolve_albums_for_track()
- filter_results_by_artist()
- search_library_with_fallback()
- search_with_alternative_interpretation()
- search_compilations_for_track()
- filter_results_by_track_validation()
- fetch_artwork_for_items()
- build_context_message()
"""

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import (
    DiscogsSearchRequest,
    DiscogsSearchResponse,
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackItem,
    TrackReleasesResponse,
)
from discogs.service import DiscogsService
from generated.api_models import (
    DiscogsReleaseInfo,
    DiscogsTrackReleasesResponse,
    TrackMatchSource,
)
from lookup.artwork import _resolve_fallback_artwork, fetch_artwork_for_items
from lookup.matching import MAX_SEARCH_RESULTS, artist_matches_item, filter_results_by_artist
from lookup.orchestrator import (
    build_context_message,
    resolve_albums_for_track,
)
from lookup.release_resolution import ResolvedRelease
from lookup.rowless import ROWLESS_LIBRARY_ID, ROWLESS_NO_ALBUM_CONFIDENCE
from lookup.strategies.artist_plus_album import search_library_with_fallback
from lookup.strategies.song_as_track import search_song_as_track
from lookup.strategies.swapped_interpretation import search_with_alternative_interpretation
from lookup.strategies.track_on_compilation import search_compilations_for_track
from lookup.validation import (
    filter_results_by_track_validation,
    find_library_albums_with_cached_track,
)
from services.parser import MessageType, ParsedRequest
from tests.factories import make_discogs_result, make_library_item

# ---------------------------------------------------------------------------
# Tests: filter_results_by_artist
# ---------------------------------------------------------------------------


class TestFilterResultsByArtist:
    """Tests for artist prefix matching."""

    def test_filters_out_non_matching_artists(self):
        results = [
            make_library_item(id=1, artist="Biz Markie", title="Young Girl Bluez"),
            make_library_item(id=2, artist="Young Black Teenagers", title="Proud to be Black"),
            make_library_item(id=3, artist="Young Gov", title="Some Album"),
        ]

        filtered = filter_results_by_artist(results, "Young Gov")

        assert len(filtered) == 1
        assert filtered[0].artist == "Young Gov"

    def test_keeps_matching_artists(self):
        results = [
            make_library_item(id=1, artist="Radiohead", title="OK Computer"),
            make_library_item(id=2, artist="Radiohead", title="The Bends"),
        ]

        filtered = filter_results_by_artist(results, "Radiohead")
        assert len(filtered) == 2

    def test_case_insensitive(self):
        results = [
            make_library_item(id=1, artist="RADIOHEAD", title="OK Computer"),
            make_library_item(id=2, artist="radiohead", title="The Bends"),
        ]

        filtered = filter_results_by_artist(results, "radiohead")
        assert len(filtered) == 2

    def test_prefix_matching_allows_various_artists(self):
        results = [
            make_library_item(id=1, artist="Various Artists - Rock - D", title="Disco Not Disco"),
        ]

        filtered = filter_results_by_artist(results, "Various")
        assert len(filtered) == 1

    def test_no_artist_returns_all(self):
        results = [
            make_library_item(id=1, artist="Radiohead", title="OK Computer"),
            make_library_item(id=2, artist="Queen", title="The Game"),
        ]

        assert len(filter_results_by_artist(results, None)) == 2
        assert len(filter_results_by_artist(results, "")) == 2

    def test_toy_does_not_match_chew_toy(self):
        results = [
            make_library_item(id=1, artist="Chew Toy", title="The Touch my Disney ep"),
            make_library_item(id=2, artist="Toy", title="Toy"),
        ]

        filtered = filter_results_by_artist(results, "Toy")
        assert len(filtered) == 1
        assert filtered[0].artist == "Toy"

    def test_bjork_with_diacritics_matches_ascii(self):
        """'Bjork' query matches library's 'Bjork' (diacritics in query, ASCII in DB)."""
        results = [make_library_item(id=1, artist="Bjork", title="Debut")]
        filtered = filter_results_by_artist(results, "Björk")
        assert len(filtered) == 1

    def test_ascii_query_matches_diacritics_artist(self):
        """'Bjork' query matches if DB somehow has 'Björk'."""
        results = [make_library_item(id=1, artist="Björk", title="Debut")]
        filtered = filter_results_by_artist(results, "Bjork")
        assert len(filtered) == 1

    def test_motorhead_diacritics(self):
        """'Motorhead' query matches library's 'Motorhead'."""
        results = [make_library_item(id=1, artist="Motorhead", title="Ace of Spades")]
        filtered = filter_results_by_artist(results, "Motörhead")
        assert len(filtered) == 1

    def test_sigur_ros_diacritics(self):
        """'Sigur Ros' query matches library's 'Sigur Ros'."""
        results = [make_library_item(id=1, artist="Sigur Ros", title="Agaetis Byrjun")]
        filtered = filter_results_by_artist(results, "Sigur Rós")
        assert len(filtered) == 1

    def test_alternate_artist_name_matches(self):
        """Item with alternate_artist_name should match when filtering by the alternate name."""
        results = [
            make_library_item(
                id=1,
                artist="Luke Vibert",
                title="Drum 'n' Bass for Papa (+ Plug EPs 1,2 & 3)",
                alternate_artist_name="Plug",
            ),
        ]
        filtered = filter_results_by_artist(results, "Plug")
        assert len(filtered) == 1

    def test_alternate_artist_name_prefix(self):
        """Prefix of alternate name should also match."""
        results = [
            make_library_item(
                id=1,
                artist="Luke Vibert",
                title="Drum 'n' Bass for Papa",
                alternate_artist_name="Plug",
            ),
        ]
        filtered = filter_results_by_artist(results, "Plu")
        assert len(filtered) == 1

    def test_no_alternate_filtered_out(self):
        """Item without alternate_artist_name should not match a different artist."""
        results = [
            make_library_item(
                id=1,
                artist="Luke Vibert",
                title="Some Album",
            ),
        ]
        filtered = filter_results_by_artist(results, "Plug")
        assert len(filtered) == 0

    # ------------------------------------------------------------------
    # Punctuation tolerance (LML#1244) -- filter_results_by_artist delegates
    # to artist_matches_item, so it inherits the punctuation fold for free.
    # See TestArtistMatchesItem below for the exhaustive per-class coverage;
    # this is a thin smoke test confirming the delegation still holds.
    # ------------------------------------------------------------------

    def test_filters_by_artist_tolerates_punctuation(self):
        """'Melt Banana' query surfaces the catalog row filed as 'Melt-Banana'."""
        results = [
            make_library_item(id=1, artist="Melt-Banana", title="Cell Scape"),
            make_library_item(id=2, artist="Boredoms", title="Vision Creation Newsun"),
        ]
        filtered = filter_results_by_artist(results, "Melt Banana")
        assert len(filtered) == 1
        assert filtered[0].artist == "Melt-Banana"


# ---------------------------------------------------------------------------
# Tests: artist_matches_item
# ---------------------------------------------------------------------------


class TestArtistMatchesItem:
    """Tests for the artist_matches_item helper."""

    def test_primary_artist_matches(self):
        item = make_library_item(id=1, artist="Radiohead", title="OK Computer")
        assert artist_matches_item(item, "Radiohead") is True

    def test_alternate_artist_matches(self):
        item = make_library_item(
            id=1, artist="Luke Vibert", title="Album", alternate_artist_name="Plug"
        )
        assert artist_matches_item(item, "Plug") is True

    def test_neither_matches(self):
        item = make_library_item(id=1, artist="Luke Vibert", title="Album")
        assert artist_matches_item(item, "Plug") is False

    def test_prefix_matches_alternate(self):
        item = make_library_item(
            id=1, artist="Luke Vibert", title="Album", alternate_artist_name="Plug"
        )
        assert artist_matches_item(item, "Plu") is True

    def test_case_insensitive(self):
        item = make_library_item(
            id=1, artist="Luke Vibert", title="Album", alternate_artist_name="Plug"
        )
        assert artist_matches_item(item, "plug") is True

    # ------------------------------------------------------------------
    # Leading-article tolerance — bidirectional.
    #
    # Library catalogers commonly file bands without the leading "The"
    # ("Black Dog Productions" rather than "The Black Dog"). User input
    # and Discogs credits often include it. The matcher must accept the
    # asymmetry in either direction so requests like "Psil-cosyin by The
    # Black Dog" surface the catalog row.
    # ------------------------------------------------------------------

    def test_leading_the_in_query_matches_article_less_artist(self):
        """Query 'The Black Dog' matches library row stored as 'Black Dog Productions'."""
        item = make_library_item(id=274, artist="Black Dog Productions", title="Spanners")
        assert artist_matches_item(item, "The Black Dog") is True

    def test_leading_the_in_query_matches_article_less_alternate(self):
        """Query 'The Black Dog' matches library row whose alternate is 'Black Dog'."""
        item = make_library_item(
            id=68523,
            artist="Black Dog Productions",
            title="Further Vexations",
            alternate_artist_name="Black Dog",
        )
        assert artist_matches_item(item, "The Black Dog") is True

    def test_article_less_query_matches_leading_the_artist(self):
        """Reverse direction: query 'Microphones' matches library row stored as 'The Microphones'."""
        item = make_library_item(id=1, artist="The Microphones", title="The Glow Pt. 2")
        assert artist_matches_item(item, "Microphones") is True

    def test_leading_a_in_query_matches_article_less_artist(self):
        """Query 'A Tribe Called Quest' matches library row stored as 'Tribe Called Quest'."""
        item = make_library_item(id=1, artist="Tribe Called Quest", title="Midnight Marauders")
        assert artist_matches_item(item, "A Tribe Called Quest") is True

    def test_leading_an_in_query_matches_article_less_artist(self):
        """Query 'An Albatross' matches library row stored as 'Albatross'."""
        item = make_library_item(id=1, artist="Albatross", title="Eat Lightning Shit Thunder")
        assert artist_matches_item(item, "An Albatross") is True

    def test_different_articles_on_each_side_match(self):
        """Both sides carrying *different* articles still match after symmetric strip."""
        item = make_library_item(id=1, artist="The Microphones", title="The Glow Pt. 2")
        assert artist_matches_item(item, "A Microphones") is True

    def test_no_match_when_only_article_remains(self):
        """Degenerate input of just 'The' must not match arbitrary rows after stripping."""
        item = make_library_item(id=1, artist="Stereolab", title="Aluminum Tunes")
        assert artist_matches_item(item, "The") is False

    def test_article_tolerance_does_not_create_false_positive(self):
        """Article-stripping must not collapse different artists into a match."""
        item = make_library_item(id=1, artist="The Microphones", title="The Glow Pt. 2")
        assert artist_matches_item(item, "The Black Dog") is False

    # ------------------------------------------------------------------
    # cross_reference_names (WXYC/discogs-etl#334) -- catalog-recorded
    # LIBRARY_CODE_CROSS_REFERENCE aliases, pipe-joined (" | ").
    #
    # Worked example: library.db row 57833 is filed under the band name
    # "Burning Star Core" with alternate_artist_name "C.S. Yeh" -- neither
    # prefix-matches a DJ typing the member's full personal name "C. Spencer
    # Yeh". The catalog's cross-reference table links the two codes, so
    # library.db now carries that link as cross_reference_names, and
    # artist_matches_item must consult it with the same strict,
    # leading-article-tolerant prefix rule as artist/alternate_artist_name.
    # ------------------------------------------------------------------

    def test_cross_reference_name_matches(self):
        item = make_library_item(
            id=57833,
            artist="Burning Star Core",
            title='"In The Blink of an Eye" 7-inch',
            alternate_artist_name="C.S. Yeh",
            cross_reference_names="C. Spencer Yeh",
        )
        assert artist_matches_item(item, "C. Spencer Yeh") is True

    def test_cross_reference_name_prefix_matches(self):
        item = make_library_item(
            id=57833,
            artist="Burning Star Core",
            title='"In The Blink of an Eye" 7-inch',
            cross_reference_names="C. Spencer Yeh",
        )
        assert artist_matches_item(item, "C. Spencer") is True

    def test_cross_reference_names_multiple_pipe_joined(self):
        """Multiple cross-referenced codes are pipe-joined; any one must match."""
        item = make_library_item(
            id=1,
            artist="Return to Forever",
            title="Live",
            cross_reference_names="Chick Corea (Return to Forever) | Elf Power",
        )
        assert artist_matches_item(item, "Elf Power") is True
        assert artist_matches_item(item, "Chick Corea (Return to Forever)") is True

    def test_cross_reference_names_absent_falls_back_to_no_match(self):
        """No cross_reference_names means only artist/alternate_artist_name are checked."""
        item = make_library_item(id=1, artist="Burning Star Core", title="Album")
        assert artist_matches_item(item, "C. Spencer Yeh") is False

    def test_cross_reference_names_leading_article_tolerance(self):
        """The leading-article tolerance rule also applies to cross_reference_names."""
        item = make_library_item(
            id=1,
            artist="Black Dog Productions",
            title="Spanners",
            cross_reference_names="Black Dog",
        )
        assert artist_matches_item(item, "The Black Dog") is True

    def test_cross_reference_names_does_not_create_false_positive(self):
        """An unrelated typed artist must not match via cross_reference_names."""
        item = make_library_item(
            id=57833,
            artist="Burning Star Core",
            title='"In The Blink of an Eye" 7-inch',
            cross_reference_names="C. Spencer Yeh",
        )
        assert artist_matches_item(item, "Stereolab") is False

    # ------------------------------------------------------------------
    # Punctuation tolerance -- bidirectional (LML#1244).
    #
    # ``normalize_for_comparison`` (``to_match_form``) lowercases and folds
    # diacritics but preserves punctuation, so a catalog row filed with a
    # hyphen, period, apostrophe, etc. never prefix-matches the punctuation-
    # free query a listener types (and vice versa) -- SQLite FTS5 already
    # tokenizes on punctuation and retrieves the rows; the artist filter was
    # the layer discarding them. This is the punctuation analogue of #364's
    # leading-article fold: a sibling comparison layered onto the existing
    # ladder, run last, so anything that matched before the fix still does.
    # ------------------------------------------------------------------

    def test_melt_banana_hyphen_query_matches_punctuated_catalog_row(self):
        """The reported production bug: 'Melt Banana' must match the catalog
        row filed as 'Melt-Banana' (WXYC/library-metadata-lookup#1244)."""
        item = make_library_item(id=30762, artist="Melt-Banana", title="Hedgehog EP")
        assert artist_matches_item(item, "Melt Banana") is True

    @pytest.mark.parametrize(
        "punctuation_class, catalog_artist, query",
        [
            # Each class is a real WXYC library.db artist, picked with the
            # punctuation embedded (not trailing) so the pre-fix comparison
            # genuinely fails char-for-char, not just because the query is
            # shorter than the candidate. Counts are the ticket's measured
            # catalog population carrying that punctuation class.
            ("hyphen (420 artists)", "A-Ha", "A Ha"),
            ("period (484 artists)", "D.I.", "D I"),
            ("apostrophe (366 artists)", "Z'ev", "Z Ev"),
            ("ampersand (313 artists)", "13 & God", "13 God"),
            ("brackets (86 artists)", "South [UK]", "South UK"),
            ("comma (81 artists)", "If, Bwana", "If Bwana"),
            ("slash (76 artists)", "F/I", "F I"),
            ("parens (55 artists)", "ES (London)", "ES London"),
            ("exclamation (43 artists)", "Yo! Majesty", "Yo Majesty"),
        ],
    )
    def test_punctuation_class_query_matches_catalog_row(
        self, punctuation_class, catalog_artist, query
    ):
        """A punctuation-free query matches the punctuated catalog row, for
        every punctuation class actually present in the library (LML#1244)."""
        item = make_library_item(id=1, artist=catalog_artist, title="Album")
        assert artist_matches_item(item, query) is True, (
            f"{punctuation_class}: query {query!r} should match catalog row {catalog_artist!r}"
        )

    @pytest.mark.parametrize(
        "catalog_artist, query",
        [
            ("Melt Banana", "Melt-Banana"),
            ("Yo Majesty", "Yo! Majesty"),
            ("A Ha", "A-Ha"),
        ],
    )
    def test_punctuated_query_matches_punctuation_free_catalog_row(self, catalog_artist, query):
        """Reverse direction: a punctuated query (as a Discogs credit or a
        DJ's typed request might carry it) matches a catalog row filed
        without the punctuation."""
        item = make_library_item(id=1, artist=catalog_artist, title="Album")
        assert artist_matches_item(item, query) is True

    def test_punctuation_fold_does_not_create_substring_match(self):
        """Folding punctuation to a SPACE (never to nothing) must not widen
        the startswith prefix match into a substring match across word
        boundaries -- 'Cats' must not match 'Cat Stevens'."""
        item = make_library_item(id=1, artist="Cat Stevens", title="Tea for the Tillerman")
        assert artist_matches_item(item, "Cats") is False

    def test_punctuation_fold_negative_control_distinct_artists(self):
        """Two real, distinct, punctuation-carrying catalog artists must not
        collapse into a match just because both happen to carry punctuation."""
        item = make_library_item(id=1, artist="A-Trak", title="Album")
        assert artist_matches_item(item, "A-Ha") is False

    def test_all_punctuation_query_does_not_match_arbitrary_row(self):
        """Degenerate input: a query that is entirely punctuation folds to
        the empty string. Unguarded, 'anything'.startswith('') is always
        True, which would match arbitrary rows -- mirrors #364's bare-
        leading-article guard."""
        item = make_library_item(id=1, artist="Stereolab", title="Aluminum Tunes")
        assert artist_matches_item(item, "...") is False

    def test_ar_kane_period_residue_is_acceptable(self):
        """Known, accepted residue: 'AR Kane' does not match 'A.R. Kane'.

        Both fold to distinct single-letter tokens ('ar kane' vs 'a r
        kane'), so the query is not a folded prefix of the catalog row.
        Chasing this would require collapsing letter-period runs, which
        risks exactly the cross-word substring matching the space-fold is
        designed to avoid. Documented here as a pin, not a bug."""
        item = make_library_item(id=1, artist="A.R. Kane", title="69")
        assert artist_matches_item(item, "AR Kane") is False

    # ------------------------------------------------------------------
    # Trailing punctuation must not open the prefix (LML#1244 review).
    #
    # A query whose own name ENDS in punctuation ("Adult.", "Neu!", "T++")
    # loses that terminator to the fold, turning a *terminated* prefix into
    # an *open* one: "adult." matched only rows starting "adult.", but
    # "adult" prefix-matches Adult Books / Adult Mom / Adult Net. The
    # retrieval feeding those rows is real -- ``_fts_normalize("Adult.")``
    # is "adult", so ``_fallback_like_search`` builds ``artist LIKE
    # '%adult%'`` -- and ``artist_matches_item`` is the ONLY artist gate on
    # ``search_album()`` inside ``search_song_as_artist`` and on
    # ``_release_matches_library_row``, so a widened gate there binds a
    # wrong-artist library row to a Discogs release (the #400
    # metadata-contamination shape).
    #
    # So the folded rungs demand EQUALITY when the query ends in
    # punctuation, and stay an open prefix otherwise. A token-boundary rule
    # would not work: "adult books".startswith("adult ") is True.
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "query, wrong_artist",
        [
            # Real WXYC catalog artists whose names end in punctuation, each
            # paired with a different catalog artist the unguarded fold
            # wrongly admitted. Measured against the library.db snapshot:
            # 47 artists gained a cross-artist hit, worst 'T++' (+2,686).
            ("Adult.", "Adult Books"),
            ("Adult.", "Adult Rodeo"),
            ("Alaska!", "Alaska y Dinarama"),
            ("Neu!", "Neurosis"),
            ("Johnny!", "Johnny Ace"),
            ("POW!", "Power Trip"),
            ("T++", "A Tribe Called Quest"),
            ("D+", "D Mob"),
            ("K.", "K-9 Posse"),
        ],
    )
    def test_trailing_punctuation_query_does_not_open_the_prefix(self, query, wrong_artist):
        """A query ending in punctuation must not prefix-match a different
        artist that merely shares the folded stem (LML#1244 review)."""
        item = make_library_item(id=1, artist=wrong_artist, title="Album")
        assert artist_matches_item(item, query) is False, (
            f"query {query!r} must not match the unrelated artist {wrong_artist!r}"
        )

    @pytest.mark.parametrize(
        "query, catalog_artist",
        [
            # The equality form still has to admit the punctuation-only
            # difference the fix exists for, in both directions.
            ("Alaska!", "Alaska"),
            ("Mark-Almond", "Mark Almond"),
            ("SW.", "SW"),
            ("Adult.", "Adult."),
        ],
    )
    def test_trailing_punctuation_query_still_matches_its_own_artist(self, query, catalog_artist):
        """The equality rung keeps the punctuation fold working for a query
        that ends in punctuation -- it narrows the rung, it does not
        disable it."""
        item = make_library_item(id=1, artist=catalog_artist, title="Album")
        assert artist_matches_item(item, query) is True

    @pytest.mark.parametrize("wrong_artist", ["Rasputina", "Rascals", "Ras G"])
    def test_trailing_underscore_is_a_terminator_too(self, wrong_artist):
        """A trailing '_' has to narrow the folded rung exactly like any other
        terminator (LML#1244 review).

        The fold spells '_' out because Python's ``\\w`` counts it as a word
        character while ``wxyc_etl``'s Rust fold treats it as punctuation. The
        trailing-terminator check has to make the same exception, or the two
        regexes in one module disagree about what punctuation is: 'Ras_' folds
        to 'ras' -- the terminator erased -- while ``ends_in_punctuation``
        reports False, leaving the rung an open prefix. That is precisely the
        terminated-prefix-becomes-open-prefix failure the guard exists to
        prevent, leaking through the one character the guard forgot.
        """
        item = make_library_item(id=1, artist=wrong_artist, title="Album")
        assert artist_matches_item(item, "Ras_") is False, (
            f"query 'Ras_' must not open-prefix the unrelated artist {wrong_artist!r}"
        )

    def test_trailing_underscore_query_still_matches_its_own_artist(self):
        """Narrowing to equality must not stop 'Ras_' reaching 'Ras G'-style
        folding of its own name -- the fold still applies, it is just exact."""
        item = make_library_item(id=1, artist="Ras", title="Album")
        assert artist_matches_item(item, "Ras_") is True

    # ------------------------------------------------------------------
    # The fold must not manufacture a leading article (LML#1244 review).
    #
    # Folding before stripping lets `strip_leading_article` see an initial
    # that the fold itself just separated: "A-Ha" -> "a ha" -> "ha", an open
    # two-character prefix that reaches Habib Koite and 240 others. The "A"
    # in "A-Ha" or "A.C. Newman" is part of the name, never an article. The
    # query side therefore strips first and folds second -- the order
    # ``wxyc_etl``'s own ``to_identity_match_form_with_punctuation`` uses.
    # The candidate side keeps fold-then-strip, so a catalog row filed
    # "The.Black Dog" is still reachable from a query "Black Dog".
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "query, wrong_artist",
        [
            ("A-Ha", "Habib Koite"),
            ("A-Ha", "Haco"),
            ("A&E", "E-40"),
            ("A&E", "An Emotional Fish"),
            ("A.C. Newman", "C Average"),
            ("A-Trak", "Trans Am"),
        ],
    )
    def test_fold_does_not_manufacture_a_leading_article(self, query, wrong_artist):
        """An initial the fold separates is not an article to strip."""
        item = make_library_item(id=1, artist=wrong_artist, title="Album")
        assert artist_matches_item(item, query) is False, (
            f"query {query!r} must not reach {wrong_artist!r} via a manufactured article"
        )

    @pytest.mark.parametrize(
        "query, catalog_artist",
        [
            ("A E", "A&E"),
            ("M Sixty", "A.M. Sixty"),
        ],
    )
    def test_candidate_side_manufactured_article_is_tolerated(self, query, catalog_artist):
        """The candidate side folds THEN strips, so it can manufacture an
        article the catalog name never had ("A&E" -> "a e" -> "e").

        Pinned rather than fixed (LML#1244 review). It lands on same-artist
        recoveries -- a listener typing "A E" should reach "A&E" -- and the
        candidate is the side being prefix-matched, so a shorter stem admits
        fewer queries, not more. Both cases here still pass after LML#1250:
        they land on the article rung's EQUALITY branch (query and candidate
        reduce to the identical stem), which LML#1250's minimum-length floor
        never gates -- only the open-prefix continuation branch is floored.
        """
        item = make_library_item(id=1, artist=catalog_artist, title="Album")
        assert artist_matches_item(item, query) is True

    def test_catalog_side_article_attached_to_punctuation_is_still_reachable(self):
        """The candidate side still folds before stripping, so a cataloger
        filing 'The.Black Dog' stays reachable from a query 'Black Dog' --
        the #364 case the folded article rung exists to serve."""
        item = make_library_item(id=1, artist="The.Black Dog", title="Bytes")
        assert artist_matches_item(item, "Black Dog") is True

    @pytest.mark.parametrize(
        "catalog_artist, query",
        [
            ("Super_Collider", "Super Collider"),
            ("CD_Slopper", "CD Slopper"),
            ("I_LIKE_DOG_FACE", "I like dog face"),
        ],
    )
    def test_underscore_folds_like_other_punctuation(self, catalog_artist, query):
        """Underscore is punctuation to a listener, and to ``wxyc_etl``'s own
        fold -- but Python's ``\\w`` counts it as a word character, so it has
        to be folded explicitly. Eight catalog artists carry one, and two of
        them are the same artist filed both ways."""
        item = make_library_item(id=1, artist=catalog_artist, title="Album")
        assert artist_matches_item(item, query) is True

    def test_trailing_punctuation_query_still_prefix_matches_via_the_as_is_rung(self):
        """The genuine continuation case survives: 'D.I.' still reaches
        'D.I. Go Pop', because the untouched as-is rung -- not the folded
        one -- is what covers a query and row sharing their punctuation."""
        item = make_library_item(id=1, artist="D.I. Go Pop", title="Album")
        assert artist_matches_item(item, "D.I.") is True

    # ------------------------------------------------------------------
    # Short article-stem wildcard guard (LML#1250).
    #
    # The leading-article rung (#364) and its folded counterpart (#1244)
    # strip an article and then open-prefix whatever stem remains, with no
    # floor on how short that stem may be. "A Ha" strips to "ha", which
    # prefix-matched 243 unrelated catalog artists on main; "A E" strips to
    # "e", which prefix-matched 796. Measured against the library.db
    # snapshot, neither lever alone closes this: a token-boundary rule
    # ("ha" must be followed by a space, not just any character) resolves
    # the within-word case ("ha" into "habib") but not the one-character
    # case -- folding punctuation can manufacture a *genuine* token
    # boundary around a single letter ("E-40" folds to "e 40", and "e 40"
    # really does start with "e "), so boundary alone still let "A E"
    # reach "E-40". A minimum stem length (>=2 characters) is what closes
    # that residual gap; floor alone (no boundary) leaves "A Ha" -> "ha"
    # untouched, since 2 characters cleared a 2-char floor. Both rules gate
    # only the open-prefix continuation branch, never an exact match -- a
    # genuinely short catalog artist is still reachable by typing it
    # verbatim (see test_short_article_stem_still_matches_its_own_name).
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "query, wrong_artist",
        [
            ("A Ha", "Habib Koite"),
            ("A E", "E-40"),
        ],
    )
    def test_short_article_stem_does_not_wildcard_match(self, query, wrong_artist):
        """The ticket's two reproducers (LML#1250)."""
        item = make_library_item(id=1, artist=wrong_artist, title="Album")
        assert artist_matches_item(item, query) is False

    def test_single_letter_query_does_not_wildcard_an_article_prefixed_candidate(self):
        """Every catalog artist filed with a leading article strips down to a
        candidate the query's stem then open-prefixes -- 'A Minor Forest'
        strips to 'minor forest', which a bare single-letter query 'M' must
        not open-prefix. A single-letter query reaches dozens of catalog
        artists this way; found via the catalog-wide sweep this ticket
        requires, not one of the two named repro cases."""
        item = make_library_item(id=1, artist="A Minor Forest", title="Album")
        assert artist_matches_item(item, "M") is False

    def test_short_article_stem_still_matches_its_own_name(self):
        """The floor narrows the open-prefix continuation, not the rung --
        an exact match at a short stem still works, in both directions."""
        item = make_library_item(id=1, artist="Ha", title="Album")
        assert artist_matches_item(item, "A Ha") is True
        item = make_library_item(id=2, artist="E", title="Album")
        assert artist_matches_item(item, "A E") is True


# ---------------------------------------------------------------------------
# Tests: build_context_message
# ---------------------------------------------------------------------------


class TestBuildContextMessage:
    """Tests for context message generation."""

    def test_compilation_context(self):
        parsed = ParsedRequest(
            song="Test Song",
            artist="Test Artist",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        context = build_context_message(parsed, found_on_compilation=True, song_not_found=False)
        assert context == 'Found "Test Song" by Test Artist on:'

    def test_album_not_found_context(self):
        parsed = ParsedRequest(
            song="Test Song",
            artist="Test Artist",
            album="Test Album",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        context = build_context_message(parsed, found_on_compilation=False, song_not_found=True)
        assert "not found in the library" in context
        assert "Test Artist" in context

    def test_song_not_found_context(self):
        parsed = ParsedRequest(
            song="Test Song",
            artist="Test Artist",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        context = build_context_message(parsed, found_on_compilation=False, song_not_found=True)
        assert "is not on any album" in context

    def test_returns_none_when_normal(self):
        parsed = ParsedRequest(
            song="Test Song",
            artist="Test Artist",
            album="Test Album",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        assert build_context_message(parsed, False, False) is None

    def test_no_results_context(self):
        parsed = ParsedRequest(
            song="Test Song",
            artist="Test Artist",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        context = build_context_message(parsed, False, True, has_results=False)
        assert "not found in library" in context


# ---------------------------------------------------------------------------
# Tests: resolve_albums_for_track
# ---------------------------------------------------------------------------


class TestResolveAlbumsForTrack:
    """Tests for Discogs album resolution."""

    @pytest.mark.asyncio
    async def test_returns_album_when_already_provided(self):
        parsed = ParsedRequest(
            song="Bohemian Rhapsody",
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        albums, not_found = await resolve_albums_for_track(parsed)
        assert albums == ["A Night at the Opera"]
        assert not_found is False

    @pytest.mark.asyncio
    async def test_looks_up_album_when_missing(self, mock_discogs_service):
        parsed = ParsedRequest(
            song="Percolator",
            artist="Stereolab",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Stereolab", "Emperor Tomato Ketchup"), ("Stereolab", "Noises [EP]")],
        ):
            albums, not_found = await resolve_albums_for_track(parsed, mock_discogs_service)

        assert "Emperor Tomato Ketchup" in albums
        assert "Noises [EP]" in albums
        assert not_found is False

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_discogs_results(self, mock_discogs_service):
        parsed = ParsedRequest(
            song="Unknown Song",
            artist="Unknown Artist",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            albums, not_found = await resolve_albums_for_track(parsed, mock_discogs_service)

        assert albums == []
        assert not_found is True

    @pytest.mark.asyncio
    async def test_skips_lookup_without_artist(self):
        """Without artist, skip Discogs lookup (results are unreliable)."""
        parsed = ParsedRequest(
            song="Laid Back",
            raw_message="Laid Back",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        albums, not_found = await resolve_albums_for_track(parsed)
        assert albums == []
        assert not_found is False

    @pytest.mark.asyncio
    async def test_filters_releases_by_diacritics_artist(self, mock_discogs_service):
        """Discogs returns 'Björk' but query artist is 'Björk' - should match."""
        parsed = ParsedRequest(
            song="Army of Me",
            artist="Björk",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Bjork", "Post"), ("Bjork", "Debut")],
        ):
            albums, not_found = await resolve_albums_for_track(parsed, mock_discogs_service)

        assert "Post" in albums
        assert not_found is False

    @pytest.mark.asyncio
    async def test_treats_album_equals_artist_as_missing(self, mock_discogs_service):
        """When parser sets album = artist name, treat as missing."""
        parsed = ParsedRequest(
            song="Test Song",
            artist="Stereolab",
            album="Stereolab",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Stereolab", "Emperor Tomato Ketchup")],
        ):
            albums, not_found = await resolve_albums_for_track(parsed, mock_discogs_service)

        assert "Emperor Tomato Ketchup" in albums

    @pytest.mark.asyncio
    async def test_non_library_artist_returns_song_not_found_with_zero_validations(self):
        """Acceptance (b) (LML#866): through the real seam, a non-library artist
        does one Discogs search and zero tracklist fetches.

        Exercises the REAL ``lookup_releases_by_track`` (not patched): the
        library-first gate reads ``db.search`` (empty), so validation is skipped
        entirely and album resolution reports song-not-found.
        """
        parsed = ParsedRequest(
            song="Some Track",
            artist="Some Non-Library Artist",
            raw_message="Some Non-Library Artist - Some Track",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        discogs_service = AsyncMock()
        discogs_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Some Track",
                artist="Some Non-Library Artist",
                releases=[
                    ReleaseInfo(
                        album=f"Album {i}",
                        artist="Some Non-Library Artist",
                        release_id=5000 + i,
                        release_url=f"https://discogs.com/release/{5000 + i}",
                    )
                    for i in range(3)
                ],
                total=3,
            )
        )
        discogs_service.validate_track_on_release = AsyncMock(return_value=True)

        db = AsyncMock()
        db.search = AsyncMock(return_value=[])  # artist not in library

        albums, not_found = await resolve_albums_for_track(parsed, discogs_service, db=db)

        assert albums == []
        assert not_found is True
        discogs_service.validate_track_on_release.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: search_library_with_fallback
# ---------------------------------------------------------------------------


class TestSearchLibraryWithFallback:
    """Tests for the multi-step library search."""

    @pytest.mark.asyncio
    async def test_finds_by_artist_plus_album(self, mock_library_db):
        item = make_library_item(
            id=1,
            artist="Queen",
            title="A Night at the Opera",
            call_letters="Q",
        )
        mock_library_db.search.return_value = [item]

        parsed = ParsedRequest(
            song="Bohemian Rhapsody",
            artist="Queen",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(
            mock_library_db, parsed, ["A Night at the Opera"]
        )
        assert len(results) == 1
        assert fallback is False

    @pytest.mark.asyncio
    async def test_falls_back_to_artist_only_when_no_discogs_albums(self, mock_library_db):
        """Falls back to artist-only when Discogs found no albums (empty list).

        With no typed album, the #400 album-match floor does not fire and the
        cascade returns the artist-only candidate as before. The with-typed-
        album-but-no-library-match shape is pinned in
        ``tests/unit/test_album_match_floor.py``.
        """
        item = make_library_item(
            id=2,
            artist="Queen",
            title="The Game",
            call_letters="Q",
            release_call_number=2,
        )
        mock_library_db.search.side_effect = [
            [],  # artist + song
            [item],  # artist only
        ]

        parsed = ParsedRequest(
            song="Test Song",
            artist="Queen",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(mock_library_db, parsed, [])
        assert len(results) == 1
        assert fallback is True

    @pytest.mark.asyncio
    async def test_artist_fallback_when_discogs_albums_not_in_library(self, mock_library_db):
        """When Discogs found specific albums but none are in library, artist fallback runs.

        Scenario: "flow coma by 808 state" — Discogs finds the track on
        "The Best Of 808 State: Blueprint" but the library doesn't have it.
        The artist+song fallback returns the "808 State" self-titled album,
        which is a false positive.  filter_results_by_track_validation()
        (tested separately in TestFilterResultsByTrackValidation) rejects it
        because "Flow Coma" isn't on the self-titled album.
        """
        false_positive = make_library_item(
            id=958,
            artist="808 State",
            title="808 State",
            call_letters="Ei",
        )
        mock_library_db.search.side_effect = [
            [],  # album search: no match for "The Best Of 808 State: Blueprint"
            [false_positive],  # artist+song: "808 State Flow Coma" matches via fuzzy
            [false_positive],  # artist-only: "808 State" matches
        ]

        parsed = ParsedRequest(
            song="Flow Coma",
            artist="808 State",
            raw_message="flow coma by 808 state",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(
            mock_library_db, parsed, ["The Best Of 808 State: Blueprint"]
        )
        # Artist+song fallback now returns the false positive; it's the job of
        # filter_results_by_track_validation() to reject it downstream.
        assert len(results) == 1
        assert results[0].title == "808 State"
        assert fallback is True
        assert mock_library_db.search.call_count == 2

    @pytest.mark.asyncio
    async def test_falls_back_to_artist_when_discogs_albums_not_in_library(self, mock_library_db):
        """When Discogs found only singles/EPs not in library, fall through to artist search.

        Bug: "6 underground by sneaker pimps" — Discogs only returns singles
        (6 Underground (Rewired), 6 Underground) which aren't in the library.
        The artist-only fallback should still run, returning all Sneaker Pimps
        albums.  filter_results_by_track_validation() (called by perform_lookup)
        then validates each against Discogs tracklists to keep only Becoming X.
        """
        becoming_x = make_library_item(id=1, artist="Sneaker Pimps", title="Becoming X")
        kiss_swallow = make_library_item(
            id=2, artist="Sneaker Pimps", title="Kiss & Swallow", release_call_number=2
        )
        mock_library_db.search.side_effect = [
            [],  # album search for "6 Underground (Rewired)" → no match
            [],  # album search for "6 Underground" → no match
            [],  # artist+song "Sneaker Pimps 6 Underground" → no match
            [becoming_x, kiss_swallow],  # artist-only "Sneaker Pimps" → both albums
        ]

        parsed = ParsedRequest(
            song="6 Underground",
            artist="Sneaker Pimps",
            raw_message="6 underground by sneaker pimps",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(
            mock_library_db,
            parsed,
            ["6 Underground (Rewired)", "6 Underground"],
        )
        assert len(results) == 2, (
            "Artist-only fallback should return both Sneaker Pimps albums; "
            "filter_results_by_track_validation handles false-positive filtering"
        )
        assert fallback is True

    @pytest.mark.asyncio
    async def test_filters_results_by_album_title(self, mock_library_db):
        """Regression: 'Wireless' album search should not also return 'Stator'."""
        mock_library_db.search.return_value = [
            make_library_item(
                id=1,
                artist="Biosphere",
                title="Wireless",
                call_letters="B",
            ),
            make_library_item(
                id=2,
                artist="Biosphere",
                title="Stator",
                call_letters="B",
                release_call_number=2,
            ),
        ]

        parsed = ParsedRequest(
            song="The Things I Tell You",
            artist="Biosphere",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(
            mock_library_db, parsed, ["Wireless - Live At The Arnolfini, Bristol"]
        )
        assert len(results) == 1
        assert results[0].title == "Wireless"

    @pytest.mark.asyncio
    async def test_self_titled_album_matches_artist_name(self, mock_library_db):
        """Self-titled albums stored as 'S/t' should match when Discogs resolves the artist name.

        Bug: "Again and Again by The Bird and the Bee" — Discogs resolves the album
        as "The Bird and the Bee" (self-titled). The library stores it as "S/t" at
        BI 125/1. The album title word filter rejects "S/t" because it has no
        significant words, so the self-titled album is never returned.
        """
        self_titled = make_library_item(
            id=50086,
            artist="The Bird and the Bee",
            title="S/t",
            call_letters="BI",
            artist_call_number=125,
            release_call_number=1,
        )
        other_album = make_library_item(
            id=54095,
            artist="The Bird and the Bee",
            title="Please Clap Your Hands",
            call_letters="BI",
            artist_call_number=125,
            release_call_number=2,
        )
        mock_library_db.search.return_value = [self_titled, other_album]

        parsed = ParsedRequest(
            song="Again and Again",
            artist="The Bird and the Bee",
            raw_message="Again and Again by The Bird and the Bee",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(
            mock_library_db, parsed, ["The Bird and the Bee"]
        )
        # The self-titled album should be included because "S/t" means the
        # album name is the same as the artist name.
        assert any(r.title == "S/t" for r in results), (
            "Self-titled album 'S/t' should match when Discogs album is the artist name"
        )
        assert fallback is False

    @pytest.mark.asyncio
    async def test_multiple_albums_all_searched_and_deduplicated(self, mock_library_db):
        """All albums should be searched and results deduplicated by ID."""
        item1 = make_library_item(id=1, artist="Stereolab", title="Emperor Tomato Ketchup")
        item2 = make_library_item(
            id=2, artist="Stereolab", title="Dots and Loops", release_call_number=2
        )
        mock_library_db.search.side_effect = [
            [item1],  # first album search
            [item2],  # second album search
        ]

        parsed = ParsedRequest(
            song="Percolator",
            artist="Stereolab",
            raw_message="Test",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        results, fallback = await search_library_with_fallback(
            mock_library_db, parsed, ["Emperor Tomato Ketchup", "Dots and Loops"]
        )

        assert len(results) == 2
        assert fallback is False
        # Primary album should be sorted first
        assert results[0].title == "Emperor Tomato Ketchup"
        assert mock_library_db.search.call_count == 2

    @pytest.mark.asyncio
    async def test_song_title_match_beats_primary_album_for_ranking(self, mock_library_db):
        """When Discogs returns multiple albums for a track and one library
        candidate's title matches the requested song, that candidate sorts
        first regardless of which album Discogs returned first.

        Reproducer: "Meet Me in the City by Junior Kimbrough". The PG cache
        ranks both Discogs releases at similarity 1.0 (both contain a track
        literally named "Meet Me in the City"); the physical-row tiebreak is
        non-deterministic, so the compilation may arrive first in `albums`.
        Sorting by `albums[0] in title` alone would let the compilation win
        even though the library has an album whose title is the song name.
        """
        meet_me_album = make_library_item(
            id=51752,
            artist="Junior Kimbrough",
            title="Meet Me in the City",
            call_letters="KI",
            artist_call_number=6,
            release_call_number=4,
        )
        you_better_run = make_library_item(
            id=51753,
            artist="Junior Kimbrough",
            title="You Better Run (The essential Junior Kimbrough)",
            call_letters="KI",
            artist_call_number=6,
            release_call_number=5,
        )
        # albums[0] is the compilation: per-album search for "You Better Run..."
        # returns the comp; per-album search for "Meet Me in the City" returns
        # the title-match album. This is the bug-triggering arrival order.
        mock_library_db.search.side_effect = [[you_better_run], [meet_me_album]]

        parsed = ParsedRequest(
            song="Meet Me in the City",
            artist="Junior Kimbrough",
            raw_message="Meet Me in the City Junior Kimbrough",
            is_request=True,
            message_type=MessageType.REQUEST,
        )
        results, fallback = await search_library_with_fallback(
            mock_library_db,
            parsed,
            [
                "You Better Run (The essential Junior Kimbrough)",
                "Meet Me in the City",
            ],
        )

        assert [r.title for r in results] == [
            "Meet Me in the City",
            "You Better Run (The essential Junior Kimbrough)",
        ]
        assert fallback is False

    @pytest.mark.asyncio
    async def test_album_only_search_when_no_artist(self, mock_library_db):
        """When parser extracts album but no artist, search by album title alone.

        Bug: "Keep On Climbin' from DJ-Kicks: Honey Dijon" parses as
        artist=None, album="DJ-Kicks: Honey Dijon", song="Keep On Climbin'".
        The library has "DJ Kicks: Honey Dijon" (no hyphen). Without an artist,
        search_library_with_fallback should still search for the album.
        """
        dj_kicks = make_library_item(
            id=71708,
            artist="Various Artists - Electronic - D",
            title="DJ Kicks: Honey Dijon",
        )
        mock_library_db.search.return_value = [dj_kicks]

        parsed = ParsedRequest(
            song="Keep On Climbin'",
            artist=None,
            album="DJ-Kicks: Honey Dijon",
            raw_message="Keep On Climbin' from DJ-Kicks: Honey Dijon",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(
            mock_library_db, parsed, ["DJ-Kicks: Honey Dijon"]
        )
        assert len(results) == 1
        assert results[0].title == "DJ Kicks: Honey Dijon"

    @pytest.mark.asyncio
    async def test_artist_only_surfaces_row_via_cross_reference_name(self, mock_library_db):
        """WXYC/discogs-etl#334 end-to-end: an artist-only lookup for the
        cataloger-cross-referenced personal name "C. Spencer Yeh" keeps the
        library.db row 57833-shaped candidate (filed under the band name
        "Burning Star Core") that the artist-only fallback search surfaces.

        db.search is mocked (retrieval is not under test here -- library_fts
        does not index cross_reference_names, see
        tests/integration/test_cross_reference_names.py); what changed is
        that the subsequent artist_matches_item filter, applied by
        search_library_with_fallback's artist-only branch, now accepts this
        candidate via its cross_reference_names instead of dropping it.
        """
        item = make_library_item(
            id=57833,
            artist="Burning Star Core",
            title='"In The Blink of an Eye" 7-inch',
            alternate_artist_name="C.S. Yeh",
            cross_reference_names="C. Spencer Yeh",
        )
        mock_library_db.search.return_value = [item]

        parsed = ParsedRequest(
            artist="C. Spencer Yeh",
            raw_message="C. Spencer Yeh - In the Blink of an Eye",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(mock_library_db, parsed, [])

        assert fallback is True
        assert len(results) == 1
        assert results[0].id == 57833

    @pytest.mark.asyncio
    async def test_underscore_catalog_title_matches_spaced_album_query(self, mock_library_db):
        """LML#1257: the per-album word-overlap check in ``search_one_album``
        now folds punctuation through the shared LML#1244 policy, which folds
        ``_`` to a space -- the old inline ``re.sub(r"[^\\w\\s]", " ", ...)``
        left ``_`` glued to its neighbors (Python's ``\\w`` treats it as a
        word character).

        Reproduces catalog row 45585, filed "Super_Collider" by
        "Super_Collider" -- one of the 8 real WXYC catalog artists whose name
        contains ``_`` (measured against the real ``library.db``,
        2026-08-22). Before this fold, the catalog title normalized to ONE
        15-char token ("super_collider") that a normally-spaced typed album
        ("super collider") could never ``startswith``-match. Folding ``_`` to
        a space splits the catalog title into two tokens ("super",
        "collider"), which the typed album's own two tokens now match
        exactly.
        """
        item = make_library_item(
            id=45585,
            artist="Super_Collider",
            title="Super_Collider",
            call_letters="S",
        )

        async def search(query, limit=None, **_):
            # Only the combined artist+album query (search_one_album's own
            # query shape) surfaces the row; the artist-only fallback query
            # is starved so a positive result can only come from the
            # word-overlap check this test targets.
            if query == "Super_Collider Super Collider":
                return [item]
            return []

        mock_library_db.search = AsyncMock(side_effect=search)

        parsed = ParsedRequest(
            artist="Super_Collider",
            raw_message="Super_Collider - Super Collider",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results, fallback = await search_library_with_fallback(
            mock_library_db, parsed, ["Super Collider"]
        )

        assert len(results) == 1
        assert results[0].id == 45585
        assert fallback is False


# ---------------------------------------------------------------------------
# Tests: search_with_alternative_interpretation
# ---------------------------------------------------------------------------


class TestSearchWithAlternativeInterpretation:
    """Tests for ambiguous format search."""

    @pytest.mark.asyncio
    async def test_finds_first_interpretation(self, mock_library_db):
        mock_library_db.search.side_effect = [
            [make_library_item(id=1, artist="Amps for Christ", title="Circuits")],
            [make_library_item(id=2, artist="Someone Else", title="Other Album")],
        ]

        results, _, _ = await search_with_alternative_interpretation(
            mock_library_db, "Amps for Christ", "Edward"
        )
        assert len(results) == 1
        assert results[0].artist == "Amps for Christ"

    @pytest.mark.asyncio
    async def test_deduplicates_results(self, mock_library_db):
        item = make_library_item(id=1, artist="Artist A", title="Album 1")
        mock_library_db.search.side_effect = [[item], [item]]

        results, _, _ = await search_with_alternative_interpretation(
            mock_library_db, "Artist A", "Something"
        )
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_matches(self, mock_library_db):
        mock_library_db.search.side_effect = [
            [make_library_item(id=1, artist="Wrong Artist", title="Album")],
            [make_library_item(id=2, artist="Also Wrong", title="Another")],
        ]

        results, _, _ = await search_with_alternative_interpretation(
            mock_library_db, "Nonexistent", "Unknown"
        )
        assert len(results) == 0

    @staticmethod
    def _track_releases(*releases: ReleaseInfo) -> TrackReleasesResponse:
        return TrackReleasesResponse(
            track="t", artist="a", releases=list(releases), total=len(releases), cached=False
        )

    @pytest.mark.asyncio
    async def test_narrows_to_release_containing_track(self, mock_library_db, mock_discogs_service):
        """LML#622: an ambiguous ``track, artist`` narrows to the release that
        actually contains the track, not the artist's whole discography.

        ``Today, Jefferson Airplane`` — "Today" is on *The Worst of Jefferson
        Airplane* only. Routes through the shared SONG_AS_TRACK kernel, so the
        Discogs tracklist validation is exercised on the successful narrow.
        """
        ja = [
            make_library_item(id=1, artist="Jefferson Airplane", title="Bark"),
            make_library_item(
                id=2, artist="Jefferson Airplane", title="Jefferson Airplane Takes Off"
            ),
            make_library_item(id=3, artist="Jefferson Airplane", title="Early Flight"),
            make_library_item(
                id=4, artist="Jefferson Airplane", title="Thirty Seconds over Winterland"
            ),
            make_library_item(
                id=5, artist="Jefferson Airplane", title="The Worst of Jefferson Airplane"
            ),
        ]
        # part1="Today" matches no artist; part2="Jefferson Airplane" matches all 5.
        mock_library_db.search.side_effect = [ja, ja]
        # search_album_fuzzy's exact-title pre-pass resolves the one release.
        worst = ja[4]
        mock_library_db.exact_title.side_effect = lambda title: (
            [worst] if title == "The Worst of Jefferson Airplane" else []
        )
        mock_discogs_service.search_releases_by_track = AsyncMock(
            return_value=self._track_releases(
                ReleaseInfo(
                    album="The Worst of Jefferson Airplane",
                    artist="Jefferson Airplane",
                    release_id=900,
                    release_url="https://www.discogs.com/release/900",
                    is_compilation=True,
                )
            )
        )
        mock_discogs_service.validate_track_on_release = AsyncMock(return_value=True)

        results, matched_via, _ = await search_with_alternative_interpretation(
            mock_library_db, "Today", "Jefferson Airplane", discogs_service=mock_discogs_service
        )

        assert [r.id for r in results] == [5]
        assert set(matched_via) == {5}
        hint = matched_via[5][0]
        assert hint.title == "Today"
        assert hint.source == TrackMatchSource.discogs_release
        # The narrow really went through the tracklist-validation seam.
        mock_discogs_service.validate_track_on_release.assert_awaited()

    @pytest.mark.asyncio
    async def test_artist_album_falls_back_to_artist_dump(
        self, mock_library_db, mock_discogs_service
    ):
        """When the non-artist half is an album (not a track), Discogs returns no
        release carrying it and the artist-filtered fallback is preserved."""
        rows = [
            make_library_item(id=1, artist="Jessica Pratt", title="On Your Own Love Again"),
            make_library_item(id=2, artist="Jessica Pratt", title="Quiet Signs"),
        ]
        # part1="Jessica Pratt" matches (results1); part2 query returns nothing.
        mock_library_db.search.side_effect = [rows, []]
        mock_discogs_service.search_releases_by_track = AsyncMock(
            return_value=self._track_releases()
        )

        results, matched_via, _ = await search_with_alternative_interpretation(
            mock_library_db,
            "Jessica Pratt",
            "On Your Own Love Again",
            discogs_service=mock_discogs_service,
        )

        assert {r.id for r in results} == {1, 2}
        assert matched_via == {}

    @pytest.mark.asyncio
    async def test_track_validation_rejection_falls_back_to_artist_dump(
        self, mock_library_db, mock_discogs_service
    ):
        """A candidate release whose tracklist does NOT validate yields no
        narrowing, so the artist-filtered fallback is returned with no hints."""
        ja = [
            make_library_item(id=1, artist="Jefferson Airplane", title="Bark"),
            make_library_item(
                id=5, artist="Jefferson Airplane", title="The Worst of Jefferson Airplane"
            ),
        ]
        mock_library_db.search.side_effect = [ja, ja]
        mock_library_db.exact_title.side_effect = lambda title: (
            [ja[1]] if title == "The Worst of Jefferson Airplane" else []
        )
        mock_discogs_service.search_releases_by_track = AsyncMock(
            return_value=self._track_releases(
                ReleaseInfo(
                    album="The Worst of Jefferson Airplane",
                    artist="Jefferson Airplane",
                    release_id=900,
                    release_url="https://www.discogs.com/release/900",
                    is_compilation=True,
                )
            )
        )
        mock_discogs_service.validate_track_on_release = AsyncMock(return_value=False)

        results, matched_via, _ = await search_with_alternative_interpretation(
            mock_library_db, "Today", "Jefferson Airplane", discogs_service=mock_discogs_service
        )

        assert {r.id for r in results} == {1, 5}  # full artist fallback, un-narrowed
        assert matched_via == {}
        mock_discogs_service.validate_track_on_release.assert_awaited()

    @pytest.mark.asyncio
    async def test_no_discogs_service_preserves_artist_dump(self, mock_library_db):
        """Without a Discogs service, narrowing is skipped and the legacy
        artist-filtered result is returned with an empty hint map."""
        rows = [make_library_item(id=1, artist="Jefferson Airplane", title="Bark")]
        mock_library_db.search.side_effect = [[], rows]

        results, matched_via, _ = await search_with_alternative_interpretation(
            mock_library_db, "Today", "Jefferson Airplane"
        )

        assert {r.id for r in results} == {1}
        assert matched_via == {}

    @pytest.mark.asyncio
    async def test_narrowing_excludes_other_artist_release(
        self, mock_library_db, mock_discogs_service
    ):
        """``require_artist`` keeps the narrow scoped to the identified artist:
        ``search_releases_by_track``'s under-3-results keyword supplement can
        return another artist's release, but those rows must not be surfaced."""
        jefferson = make_library_item(id=1, artist="Jefferson Airplane", title="Bark")
        # part1="Today" → no artist match; part2="Jefferson Airplane" → results2.
        mock_library_db.search.side_effect = [[], [jefferson]]
        # The Discogs track search (artist-filtered + keyword supplement) leaks a
        # wrong-artist release whose album fuzzy-matches a wrong-artist library row.
        byrds_row = make_library_item(id=10, artist="The Byrds", title="Younger Than Yesterday")
        mock_library_db.exact_title.side_effect = lambda title: (
            [byrds_row] if title == "Younger Than Yesterday" else []
        )
        mock_discogs_service.search_releases_by_track = AsyncMock(
            return_value=self._track_releases(
                ReleaseInfo(
                    album="Younger Than Yesterday",
                    artist="The Byrds",
                    release_id=901,
                    release_url="https://www.discogs.com/release/901",
                    is_compilation=False,
                )
            )
        )
        mock_discogs_service.validate_track_on_release = AsyncMock(return_value=True)

        results, matched_via, _ = await search_with_alternative_interpretation(
            mock_library_db, "Today", "Jefferson Airplane", discogs_service=mock_discogs_service
        )

        # The Byrds row is dropped; we fall back to the identified artist's rows.
        assert {r.id for r in results} == {1}
        assert all(r.id != 10 for r in results)
        assert matched_via == {}

    @pytest.mark.asyncio
    async def test_both_interpretations_branch_is_not_narrowed(
        self, mock_library_db, mock_discogs_service
    ):
        """When both readings resolve to a library artist, the union is returned
        un-narrowed with no hints and no Discogs call (documented scoping)."""
        row_a = make_library_item(id=1, artist="Stereolab", title="Dots and Loops")
        row_b = make_library_item(id=2, artist="Cat Power", title="Moon Pix")
        mock_library_db.search.side_effect = [[row_a], [row_b]]
        mock_discogs_service.search_releases_by_track = AsyncMock()

        results, matched_via, _ = await search_with_alternative_interpretation(
            mock_library_db, "Stereolab", "Cat Power", discogs_service=mock_discogs_service
        )

        assert {r.id for r in results} == {1, 2}
        assert matched_via == {}
        mock_discogs_service.search_releases_by_track.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests: filter_results_by_track_validation
# ---------------------------------------------------------------------------


class TestFilterResultsByTrackValidation:
    """Tests for Discogs track validation of fallback results."""

    @pytest.mark.asyncio
    async def test_filters_to_validated_albums(self, mock_discogs_service):
        items = [
            make_library_item(id=1, artist="Queen", title="A Night at the Opera"),
            make_library_item(id=2, artist="Queen", title="The Game"),
        ]

        search_result = make_discogs_result(
            release_id=12345,
            album="A Night at the Opera",
            artist="Queen",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[search_result])
        mock_discogs_service.validate_track_on_release.side_effect = [True, False]

        validated = await filter_results_by_track_validation(
            items, "Bohemian Rhapsody", "Queen", mock_discogs_service
        )

        assert validated is not None
        assert len(validated) == 1
        assert validated[0].title == "A Night at the Opera"

    @pytest.mark.asyncio
    async def test_joined_cache_credit_result_still_validates(self, mock_discogs_service):
        """LML#784 A2 non-regression: the PG arm now presents a multi-artist
        release as its aggregated credit ("Fust, Merce Lemon"). ``validate_one``
        reads only the result's album title and release_id, so a joined-credit
        candidate must validate exactly like the single-credit shape did."""
        items = [make_library_item(id=1, artist="Fust", title="Cup Of Loneliness / Choices")]
        search_result = make_discogs_result(
            release_id=36830641,
            album="Cup Of Loneliness / Choices",
            artist="Fust, Merce Lemon",
            artist_credits=["Fust", "Merce Lemon"],
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[search_result], cached=True, pg_served=True
        )
        mock_discogs_service.validate_track_on_release.return_value = True

        validated = await filter_results_by_track_validation(
            items, "Spangled", "Fust", mock_discogs_service
        )
        assert validated is not None
        assert len(validated) == 1
        assert validated[0].title == "Cup Of Loneliness / Choices"

    @pytest.mark.asyncio
    async def test_returns_none_without_discogs(self):
        items = [make_library_item(id=1, artist="Queen", title="A Night at the Opera")]
        result = await filter_results_by_track_validation(items, "Song", "Artist", None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_albums_validate(self, mock_discogs_service):
        items = [make_library_item(id=1, artist="Queen", title="The Game")]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        result = await filter_results_by_track_validation(
            items, "Unknown Song", "Queen", mock_discogs_service
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_breaker_shed_keeps_the_row_not_dropped_to_none(self, mock_discogs_service):
        """R2-4: when validation sheds (``DiscogsBreakerOpenError``) — the breaker
        is OPEN — a real library row must be KEPT (returned unvalidated), NOT
        dropped to ``None``. Dropping it laundered a shed into song-not-found: a
        wrong 200. The user should still get their library match, validation
        pending, during a flood."""
        from discogs.breaker import DiscogsBreakerOpenError

        items = [
            make_library_item(id=1, artist="Stereolab", title="Aluminum Tunes"),
            make_library_item(id=2, artist="Stereolab", title="Dots and Loops"),
        ]

        # Discogs search returns the matching album per library title, so both
        # rows pass the album-title gate and reach the validation probe.
        async def _search(req):
            title = req.album or ""
            return DiscogsSearchResponse(
                results=[
                    make_discogs_result(
                        release_id=hash(title) % 9999, album=title, artist="Stereolab"
                    )
                ]
            )

        mock_discogs_service.search.side_effect = _search
        # The validation probe sheds for every candidate.
        mock_discogs_service.validate_track_on_release.side_effect = DiscogsBreakerOpenError("shed")

        validated = await filter_results_by_track_validation(
            items, "Fuses", "Stereolab", mock_discogs_service
        )

        # The rows are retained (not None), so the orchestrator won't mark
        # song_not_found. Both rows that reached the shed are kept.
        assert validated is not None
        assert {i.id for i in validated} == {1, 2}

    @pytest.mark.asyncio
    async def test_shed_on_search_probe_also_keeps_the_row(self, mock_discogs_service):
        """R2-4: the shed can also come from the ``search`` probe (before
        validation). That, too, must KEEP the row rather than drop it."""
        from discogs.breaker import DiscogsBreakerOpenError

        items = [make_library_item(id=3, artist="Stereolab", title="Emperor Tomato Ketchup")]
        mock_discogs_service.search.side_effect = DiscogsBreakerOpenError("shed")

        validated = await filter_results_by_track_validation(
            items, "Cybele's Reverie", "Stereolab", mock_discogs_service
        )

        assert validated is not None
        assert {i.id for i in validated} == {3}

    @pytest.mark.asyncio
    async def test_rejects_when_discogs_returns_different_album(self, mock_discogs_service):
        """Discogs search for library album '808 State' returns 'The Best Of 808 State:
        Blueprint' — a different album that contains the track. The validation should
        reject this because the Discogs album doesn't match the library item's title.

        Bug: WXYC/library-metadata-lookup#115
        """
        items = [make_library_item(id=958, artist="808 State", title="808 State")]

        # Discogs search for album="808 State" returns a best-of compilation
        wrong_album = make_discogs_result(
            release_id=7641756,
            album="The Best Of 808 State: Blueprint",
            artist="808 State",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[wrong_album])
        # Track IS on that compilation — but it's the wrong album
        mock_discogs_service.validate_track_on_release.return_value = True

        result = await filter_results_by_track_validation(
            items, "Flow Coma", "808 State", mock_discogs_service
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_bounded_fan_out_caps_probes_via_chunked_gather(
        self, mock_discogs_service, monkeypatch
    ):
        """LML#808: a wide candidate list (the LML#808 widened artist-only
        fallback can pass more than ``MAX_SEARCH_RESULTS`` rows) must not fan
        out into an unbounded per-request Discogs probe burst. Pins the
        ``LML_SEARCH_MAX_API_CALLS`` cap: chunk 1 fully dispatches, chunk 2
        never does."""
        from wxyc_fastapi.observability import get_cache_stats_recorder, init_cache_stats

        monkeypatch.setenv("LML_SEARCH_MAX_API_CALLS", "3")
        init_cache_stats()

        n_candidates = MAX_SEARCH_RESULTS * 3
        items = [
            make_library_item(id=9000 + i, artist="Stereolab", title=f"Album {i}")
            for i in range(n_candidates)
        ]

        async def _search(req):
            get_cache_stats_recorder().record_api_call()
            return DiscogsSearchResponse(
                results=[
                    make_discogs_result(
                        release_id=50000 + hash(req.album) % 1000,
                        album=req.album,
                        artist="Stereolab",
                    )
                ]
            )

        mock_discogs_service.search.side_effect = _search
        mock_discogs_service.validate_track_on_release.return_value = False

        result = await filter_results_by_track_validation(
            items, "Some Song", "Stereolab", mock_discogs_service
        )

        # Nothing validated (validate_track_on_release always False), so the
        # result is None either way — the assertion that matters is the probe
        # count, not the return value.
        assert result is None
        assert mock_discogs_service.search.await_count == MAX_SEARCH_RESULTS, (
            f"Expected exactly {MAX_SEARCH_RESULTS} search probes (chunk 1 "
            f"fully ran, chunk 2 cap-gated), got "
            f"{mock_discogs_service.search.await_count}. LML#808."
        )


# ---------------------------------------------------------------------------
# Tests: find_library_albums_with_cached_track
# ---------------------------------------------------------------------------


class TestFindLibraryAlbumsWithCachedTrack:
    """Tests for the cache-driven promotion safety net.

    When the artist-only fallback returned albums that all fail track validation,
    this helper consults the local Discogs PG cache directly: "give me releases
    by this artist that contain this track" — and surfaces the matching WXYC
    library albums. Catches the case where the upstream Discogs query in
    `resolve_albums_for_track` missed the answer that the local cache holds.

    Reproduces: "bucky skank by lee scratch perry" returned 5 unrelated Lee Perry
    albums while skipping "Live at Maritime Hall", which the cache knows contains
    that track.
    """

    def _cache_releases(self, *titles_and_artists):
        from discogs.models import ReleaseInfo

        return [
            ReleaseInfo(
                album=album,
                artist=artist,
                release_id=1000 + i,
                release_url=f"https://discogs.com/release/{1000 + i}",
                is_compilation=False,
            )
            for i, (album, artist) in enumerate(titles_and_artists)
        ]

    @pytest.mark.asyncio
    async def test_returns_library_album_when_cache_links_track_to_release(
        self, mock_library_db, mock_discogs_service
    ):
        """Cache says 'Bucky Skank' is on a release whose title matches a WXYC
        library album by the same artist → that album is returned."""
        mock_discogs_service.cache_service = AsyncMock()
        mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
            return_value=self._cache_releases(
                ("Lee Scratch Perry Live At Maritime Hall", "Lee 'Scratch' Perry")
            )
        )

        maritime = make_library_item(
            id=12682,
            artist="Lee 'Scratch' Perry",
            title="Live at Maritime Hall",
        )
        mock_library_db.search.return_value = [maritime]

        result, _ = await find_library_albums_with_cached_track(
            mock_library_db,
            "Bucky Skank",
            "Lee 'Scratch' Perry",
            mock_discogs_service,
        )

        assert len(result) == 1
        assert result[0].id == 12682

    @pytest.mark.asyncio
    async def test_match_back_uses_corrected_artist_when_supplied(
        self, mock_library_db, mock_discogs_service
    ):
        """LML#626 two-channel: the library match-back keys on ``match_artist``
        (the library-corrected name), so a misspelled typed artist still promotes
        its catalog row — while the Discogs-cache probe stays on the typed name."""
        mock_discogs_service.cache_service = AsyncMock()
        mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
            return_value=self._cache_releases(("Aluminum Tunes", "Stereolab"))
        )
        row = make_library_item(id=42, artist="Stereolab", title="Aluminum Tunes")
        mock_library_db.search.return_value = [row]

        result, _ = await find_library_albums_with_cached_track(
            mock_library_db,
            "Cybele's Reverie",
            "Stereolb",  # typed (misspelled)
            mock_discogs_service,
            match_artist="Stereolab",  # library-corrected
        )

        assert [item.id for item in result] == [42]
        # The cache probe used the typed name, not the correction.
        probe = mock_discogs_service.cache_service.search_releases_by_track
        assert probe.await_args.kwargs["artist"] == "Stereolb"

    @pytest.mark.asyncio
    async def test_match_back_defaults_to_typed_artist_without_correction(
        self, mock_library_db, mock_discogs_service
    ):
        """Without ``match_artist`` the match-back falls back to the typed
        ``artist`` (single-channel default) — a misspelled typed name drops the
        row, which is why ``perform_lookup`` threads the corrected name in."""
        mock_discogs_service.cache_service = AsyncMock()
        mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
            return_value=self._cache_releases(("Aluminum Tunes", "Stereolab"))
        )
        row = make_library_item(id=42, artist="Stereolab", title="Aluminum Tunes")
        mock_library_db.search.return_value = [row]

        result, _ = await find_library_albums_with_cached_track(
            mock_library_db,
            "Cybele's Reverie",
            "Stereolb",  # typed (misspelled), no correction supplied
            mock_discogs_service,
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_without_discogs_service(self, mock_library_db):
        result, _ = await find_library_albums_with_cached_track(
            mock_library_db, "Song", "Artist", None
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_without_cache_service(self, mock_library_db, mock_discogs_service):
        """No PG cache attached → helper is a no-op (we don't fall back to the API)."""
        mock_discogs_service.cache_service = None
        result, _ = await find_library_albums_with_cached_track(
            mock_library_db, "Song", "Artist", mock_discogs_service
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_song_or_artist_missing(
        self, mock_library_db, mock_discogs_service
    ):
        mock_discogs_service.cache_service = AsyncMock()
        assert await find_library_albums_with_cached_track(
            mock_library_db, None, "Artist", mock_discogs_service
        ) == ([], {})
        assert await find_library_albums_with_cached_track(
            mock_library_db, "Song", None, mock_discogs_service
        ) == ([], {})
        # Cache should never be consulted when inputs are insufficient
        mock_discogs_service.cache_service.search_releases_by_track.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_empty_when_cache_has_no_matching_releases(
        self, mock_library_db, mock_discogs_service
    ):
        mock_discogs_service.cache_service = AsyncMock()
        mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(return_value=[])
        result, _ = await find_library_albums_with_cached_track(
            mock_library_db, "Song", "Artist", mock_discogs_service
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_releases_with_no_library_match(
        self, mock_library_db, mock_discogs_service
    ):
        """Cache returns a release the WXYC library doesn't carry."""
        mock_discogs_service.cache_service = AsyncMock()
        mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
            return_value=self._cache_releases(("Some Bootleg Compilation", "Lee Perry"))
        )
        mock_library_db.search.return_value = []

        result, _ = await find_library_albums_with_cached_track(
            mock_library_db, "Song", "Lee Perry", mock_discogs_service
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_filters_out_library_rows_with_wrong_artist(
        self, mock_library_db, mock_discogs_service
    ):
        """FTS returns an album with a matching title but a different artist."""
        mock_discogs_service.cache_service = AsyncMock()
        mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
            return_value=self._cache_releases(("Live at Maritime Hall", "Lee Perry"))
        )
        wrong_artist = make_library_item(
            id=99, artist="Some Other Band", title="Live at Maritime Hall"
        )
        mock_library_db.search.return_value = [wrong_artist]

        result, _ = await find_library_albums_with_cached_track(
            mock_library_db, "Bucky Skank", "Lee 'Scratch' Perry", mock_discogs_service
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_dedupes_when_multiple_releases_resolve_to_same_library_row(
        self, mock_library_db, mock_discogs_service
    ):
        """Two cache releases for the same album (different pressings) → one row."""
        mock_discogs_service.cache_service = AsyncMock()
        mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
            return_value=self._cache_releases(
                ("Live at Maritime Hall", "Lee 'Scratch' Perry"),
                ("Lee Scratch Perry Live At Maritime Hall", "Lee 'Scratch' Perry"),
            )
        )
        maritime = make_library_item(
            id=12682, artist="Lee 'Scratch' Perry", title="Live at Maritime Hall"
        )
        mock_library_db.search.return_value = [maritime]

        result, _ = await find_library_albums_with_cached_track(
            mock_library_db,
            "Bucky Skank",
            "Lee 'Scratch' Perry",
            mock_discogs_service,
        )
        assert len(result) == 1
        assert result[0].id == 12682

    @pytest.mark.asyncio
    async def test_cache_failure_returns_empty_does_not_raise(
        self, mock_library_db, mock_discogs_service
    ):
        """Cache exceptions degrade gracefully — caller still gets a usable answer."""
        mock_discogs_service.cache_service = AsyncMock()
        mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
            side_effect=RuntimeError("cache offline")
        )
        result, _ = await find_library_albums_with_cached_track(
            mock_library_db,
            "Song",
            "Artist",
            mock_discogs_service,
        )
        assert result == []


# ---------------------------------------------------------------------------
# Tests: row-less no-album soft confidence (A2, LML#629)
# ---------------------------------------------------------------------------


class TestRowlessNoAlbumConfidence:
    """A no-album (song-only / artist+song) query that surfaces a row-less
    non-library release should carry a *soft* confidence so a consumer can treat
    it as tentative — the artist + track were validated, but with no typed album
    the chosen release is the best-ranked guess, not a user-confirmed one. When
    an album *was* typed, the bound release keeps full confidence (1.0)."""

    @pytest.fixture
    def enable_nonlibrary_release(self, monkeypatch):
        monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "true")
        from config.settings import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def _svc(self) -> AsyncMock:
        svc = AsyncMock()
        svc.cache_service = None
        svc.get_release = AsyncMock(
            return_value=ReleaseMetadataResponse(
                release_id=1000,
                title="Roast Fish Collie Weed & Corn Bread",
                artist="Lee 'Scratch' Perry",
                release_url="https://www.discogs.com/release/1000",
                artwork_url="https://i.discogs.com/roast.jpg",
            )
        )
        return svc

    def _rowless(self, *, confidence: float = 1.0):
        item = make_library_item(
            id=ROWLESS_LIBRARY_ID,
            artist="Lee 'Scratch' Perry",
            title="Roast Fish Collie Weed & Corn Bread",
        )
        resolved = ResolvedRelease(
            release_id=1000,
            release_url="https://www.discogs.com/release/1000",
            is_compilation=False,
            album_title="Roast Fish Collie Weed & Corn Bread",
            confidence=confidence,
        )
        return item, {ROWLESS_LIBRARY_ID: resolved}

    @pytest.mark.asyncio
    async def test_no_album_rowless_bind_carries_soft_confidence(self, enable_nonlibrary_release):
        item, discogs_titles = self._rowless()
        svc = self._svc()

        results = await fetch_artwork_for_items(
            [item], svc, discogs_titles, song="Bucky Skank", album=None
        )

        bound = results[0][1]
        assert bound is not None
        assert bound.release_id == 1000
        assert bound.confidence < 1.0

    @pytest.mark.asyncio
    async def test_album_typed_rowless_bind_keeps_full_confidence(self, enable_nonlibrary_release):
        item, discogs_titles = self._rowless()
        svc = self._svc()

        results = await fetch_artwork_for_items(
            [item],
            svc,
            discogs_titles,
            song="Bucky Skank",
            album="Roast Fish Collie Weed & Corn Bread",
        )

        bound = results[0][1]
        assert bound is not None
        assert bound.confidence == 1.0

    @pytest.mark.asyncio
    async def test_soft_seam_stays_soft_even_with_typed_album(self, enable_nonlibrary_release):
        """A row-less release whose seam confidence is already soft (the A4
        cached-track pick, never album-matched) must NOT be promoted to 1.0 just
        because the request typed an album — the bind takes the softer signal.
        Without this, A4 would stamp 'user-confirmed album' on a release it never
        matched to the typed album."""
        item, discogs_titles = self._rowless(confidence=ROWLESS_NO_ALBUM_CONFIDENCE)
        svc = self._svc()

        results = await fetch_artwork_for_items(
            [item], svc, discogs_titles, song="Bucky Skank", album="A Typed Album"
        )

        bound = results[0][1]
        assert bound is not None
        assert bound.confidence == ROWLESS_NO_ALBUM_CONFIDENCE


# ---------------------------------------------------------------------------
# Tests: fetch_artwork_for_items
# ---------------------------------------------------------------------------


class TestFetchArtworkForItems:
    """Tests for parallel artwork fetching."""

    @pytest.mark.asyncio
    async def test_fetches_artwork_for_each_item(self, mock_discogs_service):
        """Two items get their own per-search match — verifies the
        gather-of-fetch_one pattern actually fans out and zips results back
        in order. Returns distinct artwork per item via mock side_effect so
        the second item isn't a degenerate copy of the first."""
        items = [
            make_library_item(id=1, artist="Queen", title="A Night at the Opera"),
            make_library_item(id=2, artist="Queen", title="The Game"),
        ]

        opera = make_discogs_result(
            release_id=12345,
            album="A Night at the Opera",
            artist="Queen",
            artwork_url="https://example.com/opera.jpg",
        )
        game = make_discogs_result(
            release_id=67890,
            album="The Game",
            artist="Queen",
            artwork_url="https://example.com/game.jpg",
        )
        mock_discogs_service.search.side_effect = [
            DiscogsSearchResponse(results=[opera]),
            DiscogsSearchResponse(results=[game]),
        ]

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 2
        assert results[0][0].id == 1
        assert results[0][1] is not None
        assert results[0][1].release_id == 12345
        assert results[1][0].id == 2
        assert results[1][1] is not None
        assert results[1][1].release_id == 67890

    @pytest.mark.asyncio
    async def test_single_credit_artist_matches_joined_cache_credit(self, mock_discogs_service):
        """LML#784 A2 non-regression: the PG cache arm now presents a
        multi-artist release as its aggregated credit ("Fust, Merce Lemon").
        A library item credited to just one of those artists must keep
        matching via the per-credit ``artist_credits`` variants."""
        item = make_library_item(id=1, artist="Fust", title="Cup Of Loneliness / Choices")
        joined = make_discogs_result(
            release_id=36830641,
            album="Cup Of Loneliness / Choices",
            artist="Fust, Merce Lemon",
            artist_credits=["Fust", "Merce Lemon"],
            artwork_url="https://example.com/cup.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            cached=True, results=[joined]
        )

        results = await fetch_artwork_for_items([item], mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].release_id == 36830641

    @pytest.mark.asyncio
    async def test_returns_none_artwork_without_discogs(self):
        items = [make_library_item(id=1, artist="Queen", title="A Night at the Opera")]

        results = await fetch_artwork_for_items(items, None)

        assert len(results) == 1
        assert results[0][0].id == 1
        assert results[0][1] is None

    @pytest.mark.asyncio
    async def test_artwork_uses_alternate_artist(self, mock_discogs_service):
        """When item has alternate_artist_name, use it for Discogs search."""
        item = make_library_item(
            id=1,
            artist="Luke Vibert",
            title="Drum 'n' Bass for Papa",
            alternate_artist_name="Plug",
        )

        artwork = make_discogs_result(
            release_id=12345,
            album="Drum 'n' Bass for Papa",
            artist="Plug",
            artwork_url="https://example.com/cover.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[artwork])

        results = await fetch_artwork_for_items([item], mock_discogs_service)

        assert len(results) == 1
        call_args = mock_discogs_service.search.call_args[0][0]
        assert isinstance(call_args, DiscogsSearchRequest)
        assert call_args.artist == "Plug"

    @pytest.mark.asyncio
    async def test_uses_discogs_titles_for_compilation_lookup(self, mock_discogs_service):
        """For compilations, use the Discogs album title (not library title) for
        artwork — but the score floor must still accept the short library
        title when Discogs returns it, since the long discogs_titles override
        is a search aid, not a verification requirement."""
        item = make_library_item(
            id=20,
            artist="Various Artists - Rock - D",
            title="Disco Not Disco",
        )

        artwork = make_discogs_result(
            release_id=99999,
            album="Disco Not Disco",
            artist="Various",
            artwork_url="https://example.com/disco.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[artwork])

        discogs_titles = {
            20: ResolvedRelease(
                release_id=99999,
                release_url="https://www.discogs.com/release/99999",
                is_compilation=True,
                album_title="Disco Not Disco (Post Punk, Electro & Leftfield Disco Classics)",
            )
        }
        results = await fetch_artwork_for_items(
            items=[item], discogs_service=mock_discogs_service, discogs_titles=discogs_titles
        )

        assert len(results) == 1
        # Search used the long Discogs-canonical title (the override's purpose).
        call_args = mock_discogs_service.search.call_args[0][0]
        assert isinstance(call_args, DiscogsSearchRequest)
        assert "Disco Not Disco" in call_args.album
        # ALSO the artwork survived the floor against the short library title
        # (the variant the candidate carries). Without title-variant scoring,
        # the long override vs. the short candidate would score 38.5 and the
        # fuzzy-score floor would flip this to None.
        assert results[0][1] is not None
        assert results[0][1].release_id == 99999
        assert results[0][1].artwork_url == "https://example.com/disco.jpg"

    @pytest.mark.asyncio
    async def test_compilation_canonical_various_artists(self, mock_discogs_service):
        """Discogs returns most compilation releases with the canonical artist
        string "Various Artists" (sometimes with a "(N)" disambiguation
        suffix). The orchestrator mutates `artist` to bare "Various" for
        Discogs's search endpoint, but scoring must tolerate the canonical
        form — otherwise score_match("Various", "Various Artists") = 63.6 and
        every "Various Artists" compilation flips to None."""
        item = make_library_item(
            id=21,
            artist="Various Artists - Rock - D",
            title="Disco Not Disco",
        )

        candidate = make_discogs_result(
            release_id=88888,
            album="Disco Not Disco",
            artist="Various Artists",  # the canonical form, not the bare "Various"
            artwork_url="https://example.com/disco-canonical.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[candidate])

        results = await fetch_artwork_for_items([item], mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].release_id == 88888
        assert results[0][1].artwork_url == "https://example.com/disco-canonical.jpg"

    @pytest.mark.asyncio
    async def test_self_titled_does_not_leak_pattern_into_album_variants(
        self, mock_discogs_service
    ):
        """The self-titled mutation sets album=item.artist; the title-variant
        block must NOT re-append the trigger pattern ("S/t") because a stray
        Discogs result whose album is literally "S/t" would clear the floor
        trivially against the readmitted variant, defeating the floor's intent."""
        item = make_library_item(id=1, artist="Pavement", title="S/t", format="LP")

        # Wrong-release candidate that happens to carry album="S/t".
        wrong_release_with_st_album = make_discogs_result(
            release_id=999,
            album="S/t",
            artist="Pavement",
            artwork_url="https://example.com/wrong.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[wrong_release_with_st_album]
        )

        results = await fetch_artwork_for_items([item], mock_discogs_service)

        assert len(results) == 1
        # Without the self-titled-pattern filter on the variant append, the
        # variant ["Pavement", "S/t"] would let this wrong-release clear via
        # score_match("S/t", "S/t") = 100.
        assert results[0][1] is None

    @pytest.mark.asyncio
    async def test_returns_none_when_item_title_is_empty(self, mock_discogs_service):
        """Library rows with title=None or title='' (allowed by LibraryItem)
        no longer return a top-1 by default. The album variant set collapses
        to [''], the empty-variant short-circuit fires, the function returns
        None. Pin this so a future relaxation can't silently re-introduce
        the 'first Discogs hit wins regardless of title' behavior the floor
        is meant to prevent."""
        item = make_library_item(id=1, artist="Stereolab", title="", format="CD")

        # Even a candidate that perfectly matches the artist clearly fails
        # because there is no title to score against.
        candidate = make_discogs_result(
            release_id=42,
            album="Aluminum Tunes",
            artist="Stereolab",
            artwork_url="https://example.com/al.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[candidate])

        results = await fetch_artwork_for_items([item], mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is None

    @pytest.mark.asyncio
    async def test_artwork_search_passes_label_and_format(self, mock_discogs_service):
        """When item has label and format, pass them to DiscogsSearchRequest."""
        item = make_library_item(
            id=1,
            artist="Cat Power",
            title="Moon Pix",
            format="cd",
            label="Matador Records",
        )

        artwork = make_discogs_result(
            release_id=12345,
            album="Moon Pix",
            artist="Cat Power",
            artwork_url="https://example.com/cover.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[artwork])

        results = await fetch_artwork_for_items([item], mock_discogs_service)

        assert len(results) == 1
        call_args = mock_discogs_service.search.call_args[0][0]
        assert isinstance(call_args, DiscogsSearchRequest)
        assert call_args.label == "Matador Records"
        assert call_args.format == "CD"

    @pytest.mark.asyncio
    async def test_artwork_search_omits_label_and_format_when_absent(self, mock_discogs_service):
        """When item has no label, label should be None in search request."""
        item = make_library_item(id=1, artist="Cat Power", title="Moon Pix")

        artwork = make_discogs_result(release_id=12345, album="Moon Pix", artist="Cat Power")
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[artwork])

        await fetch_artwork_for_items([item], mock_discogs_service)

        call_args = mock_discogs_service.search.call_args[0][0]
        assert call_args.label is None

    @pytest.mark.asyncio
    async def test_picks_correct_release_over_misleading_top1(self, mock_discogs_service):
        """When Discogs's top-1 is the wrong release, the 80/80 floor + score
        picks the correct one further down. LML#478 — concrete repro:
        "Hebebeb (Zrag)" by Noura Mint Seymali appears on both *Tzenni* (2014)
        and *Yenbett* (2025); Discogs's popularity-ranked top-1 isn't always
        the album the DJ entered."""
        item = make_library_item(id=1, artist="Noura Mint Seymali", title="Tzenni", format="CD")

        wrong = make_discogs_result(
            release_id=99991,
            album="Yenbett",
            artist="Noura Mint Seymali",
            artwork_url="https://example.com/yenbett.jpg",
        )
        right = make_discogs_result(
            release_id=12345,
            album="Tzenni",
            artist="Noura Mint Seymali",
            artwork_url="https://example.com/tzenni.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[wrong, right])

        results = await fetch_artwork_for_items([item], mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].release_id == 12345
        assert results[0][1].album == "Tzenni"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_candidate_clears_floor(self, mock_discogs_service):
        """When every candidate's artist+album fails the 80/80 floor against
        the queried item, return None — better than serving wrong artwork
        confidently. _resolve_fallback_artwork is NOT called (no result)."""
        item = make_library_item(id=1, artist="Noura Mint Seymali", title="Tzenni", format="CD")

        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(release_id=1, album="Greatest Hits", artist="Some Other Band"),
                make_discogs_result(
                    release_id=2, album="Live in Berlin", artist="Another Wrong Artist"
                ),
            ]
        )

        results = await fetch_artwork_for_items([item], mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is None
        mock_discogs_service.get_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_titled_scores_against_mutated_album(self, mock_discogs_service):
        """Self-titled albums stored as "S/t" have `album` mutated to the
        artist name before the Discogs search; the score must use the mutated
        value (constraint 4 in LML#478)."""
        item = make_library_item(id=1, artist="Pavement", title="S/t", format="LP")

        # Candidate album is "Pavement" — matches the mutated album, NOT the raw
        # "S/t" library title. Without the mutation flowing into the score,
        # this would fail the 80/80 floor.
        candidate = make_discogs_result(
            release_id=555,
            album="Pavement",
            artist="Pavement",
            artwork_url="https://example.com/pavement.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[candidate])

        results = await fetch_artwork_for_items([item], mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].release_id == 555

    @pytest.mark.asyncio
    async def test_compilation_artist_scores_against_various(self, mock_discogs_service):
        """Compilation artists ("Various Artists - …") have `artist` mutated
        to "Various" before the Discogs search; the score must use the
        mutated value (constraint 4 in LML#478)."""
        item = make_library_item(
            id=20, artist="Various Artists - Rock - D", title="Disco Not Disco"
        )

        candidate = make_discogs_result(
            release_id=99999,
            album="Disco Not Disco",
            artist="Various",
            artwork_url="https://example.com/disco.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[candidate])

        results = await fetch_artwork_for_items([item], mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].release_id == 99999

    @pytest.mark.asyncio
    async def test_picks_higher_combined_score_among_acceptable_candidates(
        self, mock_discogs_service
    ):
        """When multiple candidates clear the 80/80 floor, pick the one with
        the highest combined (artist + title) score — regardless of Discogs's
        ordering."""
        item = make_library_item(id=1, artist="Stereolab", title="Aluminum Tunes", format="CD")

        # Both candidates pass 80/80 but the second is a near-perfect match.
        slightly_off = make_discogs_result(
            release_id=1,
            album="Aluminium Tunes",  # British spelling, off by one char
            artist="Stereolab",
            artwork_url="https://example.com/off.jpg",
        )
        exact = make_discogs_result(
            release_id=2,
            album="Aluminum Tunes",
            artist="Stereolab",
            artwork_url="https://example.com/exact.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[slightly_off, exact]
        )

        results = await fetch_artwork_for_items([item], mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].release_id == 2


def _va_comp_release(
    release_id: int = 36907527, album: str = "When There Is No Sun"
) -> ReleaseInfo:
    """A Various-Artists compilation ReleaseInfo for the LML#604 spine case."""
    return ReleaseInfo(
        album=album,
        artist="Various",
        release_id=release_id,
        release_url=f"https://www.discogs.com/release/{release_id}",
        is_compilation=True,
    )


class TestFetchArtworkLazyReleaseResolution:
    """LML#604 PR2: when the floor search rejects (returns None) and a song is
    present, fetch_one lazily resolves the Various-Artists compilation release
    the track sits on and trust-and-binds it — the symptom fix for the missing
    compilation ``discogs_url``.

    Spine throughout: A Guy Called Gerald — "Message to Black Youth" on the
    V/A compilation "When There Is No Sun" (discogs release 36907527). The
    library row is filed under the *track* artist (the root-cause shape), so
    the artist floor can never clear "Various" and the floor returns None.
    """

    @pytest.fixture
    def enable_compilation_release(self, monkeypatch):
        """Turn on lml_resolve_compilation_release for the binding fallback."""
        monkeypatch.setenv("LML_RESOLVE_COMPILATION_RELEASE", "true")
        from config.settings import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def _spine_service(self) -> AsyncMock:
        svc = AsyncMock()
        svc.cache_service = None
        # Floor search returns nothing usable -> find_best_typed_match -> None.
        svc.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
        # The lazy resolve_release_for_track probes by track and validates.
        svc.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Message to Black Youth",
                artist="A Guy Called Gerald",
                releases=[_va_comp_release()],
                total=1,
            )
        )
        svc.validate_track_on_release = AsyncMock(return_value=True)
        # Album-title wave (parity probe) — empty by default; the track wave
        # carries the spine case.
        svc.search_releases_by_album_title = AsyncMock(
            return_value=TrackReleasesResponse(track="", artist="", releases=[], total=0)
        )
        svc.get_release = AsyncMock(
            return_value=ReleaseMetadataResponse(
                release_id=36907527,
                title="When There Is No Sun",
                artist="Various",
                release_url="https://www.discogs.com/release/36907527",
                artwork_url="https://i.discogs.com/sun.jpg",
            )
        )
        return svc

    @pytest.mark.asyncio
    async def test_binds_va_compilation_release_when_floor_rejects(
        self, enable_compilation_release
    ):
        item = make_library_item(id=1, artist="A Guy Called Gerald", title="When There Is No Sun")
        svc = self._spine_service()

        results = await fetch_artwork_for_items([item], svc, song="Message to Black Youth")

        assert len(results) == 1
        bound = results[0][1]
        assert bound is not None
        assert bound.release_id == 36907527
        assert bound.release_url == "https://www.discogs.com/release/36907527"
        # Art comes from the resolved release's own cover via _resolve_fallback_artwork.
        assert bound.artwork_url == "https://i.discogs.com/sun.jpg"

    @pytest.mark.asyncio
    async def test_lazy_fallback_passes_raw_track_artist_not_various(
        self, enable_compilation_release
    ):
        """The probe must use the track artist (validated per-track), never the
        bare 'Various' search form the floor uses for compilation rows."""
        item = make_library_item(id=1, artist="A Guy Called Gerald", title="When There Is No Sun")
        svc = self._spine_service()

        await fetch_artwork_for_items([item], svc, song="Message to Black Youth")

        # search_releases_by_track was called with the real track artist.
        assert svc.search_releases_by_track.await_count >= 1
        first = svc.search_releases_by_track.await_args_list[0]
        # signature: search_releases_by_track(track, artist, *, artist_as_keyword=False)
        artist_arg = first.kwargs.get("artist")
        if artist_arg is None and len(first.args) >= 2:
            artist_arg = first.args[1]
        assert artist_arg == "A Guy Called Gerald"

    @pytest.mark.asyncio
    async def test_flag_off_leaves_floor_rejection_unbound(self):
        """Flag default-off: a floor-rejected row stays unbound (pre-PR2) and the
        lazy probe never fires — guaranteeing flag-off is byte-identical."""
        item = make_library_item(id=1, artist="A Guy Called Gerald", title="When There Is No Sun")
        svc = self._spine_service()

        results = await fetch_artwork_for_items([item], svc, song="Message to Black Youth")

        assert results[0][1] is None
        svc.search_releases_by_track.assert_not_called()

    @pytest.mark.asyncio
    async def test_lazy_fallback_applies_canonical_swap(
        self, enable_compilation_release, monkeypatch
    ):
        """Probe parity (deferred finding #1): when lml_resolve_artist_canonical
        is on and the resolver swaps, the lazy probe uses the canonical artist —
        the same swap the live search_compilations_for_track probe applies."""
        monkeypatch.setenv("LML_RESOLVE_ARTIST_CANONICAL", "true")
        from config.settings import get_settings

        get_settings.cache_clear()
        item = make_library_item(id=1, artist="A Guy Calld Gerald", title="When There Is No Sun")
        svc = self._spine_service()
        cache_service = AsyncMock()
        cache_service.search_artists_by_name = AsyncMock(
            return_value=[{"id": 1, "name": "A Guy Called Gerald", "score": 0.99}]
        )
        svc.cache_service = cache_service

        await fetch_artwork_for_items([item], svc, song="Message to Black Youth")

        artists_probed = {
            (c.kwargs.get("artist") or (c.args[1] if len(c.args) >= 2 else None))
            for c in svc.search_releases_by_track.await_args_list
        }
        assert artists_probed == {"A Guy Called Gerald"}

    @pytest.mark.asyncio
    async def test_lazy_fallback_fires_album_title_wave(self, enable_compilation_release):
        """Probe parity (deferred finding #1): a comp the track probe misses but
        the album-title probe surfaces still binds — the album-title wave runs
        (lml_resolve_artist_canonical off, so no swap → wave fires)."""
        item = make_library_item(id=1, artist="A Guy Called Gerald", title="When There Is No Sun")
        svc = self._spine_service()
        # Track probe finds nothing...
        svc.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Message to Black Youth",
                artist="A Guy Called Gerald",
                releases=[],
                total=0,
            )
        )
        # ...but the album-title probe surfaces the V/A comp.
        svc.search_releases_by_album_title = AsyncMock(
            return_value=TrackReleasesResponse(
                track="",
                artist="",
                releases=[_va_comp_release()],
                total=1,
            )
        )

        results = await fetch_artwork_for_items([item], svc, song="Message to Black Youth")

        svc.search_releases_by_album_title.assert_awaited()
        bound = results[0][1]
        assert bound is not None
        assert bound.release_id == 36907527

    @pytest.mark.asyncio
    async def test_carried_release_trust_binds_without_research(self, enable_compilation_release):
        """Flag on + a ResolvedRelease carried on the seam (TRACK_ON_COMPILATION
        already resolved it this request) → trust-and-bind by id, skipping the
        floor re-search entirely."""
        item = make_library_item(
            id=20, artist="Various Artists - Rock - D", title="Disco Not Disco"
        )
        discogs_titles = {
            20: ResolvedRelease(
                release_id=99999,
                release_url="https://www.discogs.com/release/99999",
                is_compilation=True,
                album_title="Disco Not Disco (Post Punk, Electro & Leftfield Disco Classics)",
            )
        }
        svc = AsyncMock()
        svc.cache_service = None
        svc.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
        svc.get_release = AsyncMock(
            return_value=ReleaseMetadataResponse(
                release_id=99999,
                title="Disco Not Disco",
                artist="Various",
                release_url="https://www.discogs.com/release/99999",
                artwork_url="https://i.discogs.com/disco.jpg",
            )
        )

        results = await fetch_artwork_for_items(
            [item], svc, discogs_titles=discogs_titles, song="Some Track"
        )

        bound = results[0][1]
        assert bound is not None
        assert bound.release_id == 99999
        assert bound.release_url == "https://www.discogs.com/release/99999"
        assert bound.artwork_url == "https://i.discogs.com/disco.jpg"
        # The whole point: no artist-floor re-search on the carried path.
        svc.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_negative_cache_skips_reprobe_on_second_identical_call(
        self, enable_compilation_release
    ):
        """An unresolvable row (probe found a candidate but it fails track-credit
        validation) must not re-probe Discogs on a second identical lookup — the
        L1 negative cache pins the empty result. Guards the LML#370-372
        cascade shape: a steady poll of an unbindable comp must not fan out
        every time."""
        item = make_library_item(id=1, artist="A Guy Called Gerald", title="When There Is No Sun")
        svc = self._spine_service()
        # Probe surfaces a candidate, but it fails per-track validation → [].
        svc.validate_track_on_release = AsyncMock(return_value=False)

        first = await fetch_artwork_for_items([item], svc, song="Message to Black Youth")
        probes_after_first = svc.search_releases_by_track.await_count
        second = await fetch_artwork_for_items([item], svc, song="Message to Black Youth")

        assert first[0][1] is None
        assert second[0][1] is None
        # No new track probes on the second pass — the empty result was cached.
        assert svc.search_releases_by_track.await_count == probes_after_first

    @pytest.mark.asyncio
    async def test_bulk_kill_switch_suppresses_lazy_fallback(self, enable_compilation_release):
        """allow_release_resolution_fallback=False (the /lookup/bulk drain) must
        suppress the lazy fan-out even with the flag on — the 35k-album backfill
        can never trigger a per-row Discogs probe."""
        item = make_library_item(id=1, artist="A Guy Called Gerald", title="When There Is No Sun")
        svc = self._spine_service()

        results = await fetch_artwork_for_items(
            [item], svc, song="Message to Black Youth", allow_release_resolution_fallback=False
        )

        assert results[0][1] is None
        svc.search_releases_by_track.assert_not_called()


class TestFetchArtworkRowlessBindKillSwitch:
    """LML#652: the row-less (id==0) ``bind_carried`` trust-bind respects the bulk
    kill switch. Once the four producers are gated no row-less item reaches
    ``fetch_artwork_for_items`` on bulk, so this is belt-and-suspenders — but it's
    the only level at which the ``and allow_release_resolution_fallback`` guard on
    the ``id == ROWLESS_LIBRARY_ID`` branch can be exercised directly.

    Distinct from the #604 compilation trust-bind (an in-library row whose
    ``lml_resolve_compilation_release`` carried release binds): that branch is NOT
    gated by the switch (its own lazy fallback already is) and stays bound.
    """

    @pytest.fixture
    def enable_nonlibrary_release(self, monkeypatch):
        monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "true")
        from config.settings import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def _rowless_inputs(self):
        """An id==0 row-less item + its carried release, and a service whose
        floor re-search finds nothing (so a suppressed bind leaves it unbound)."""
        item = make_library_item(id=0, artist="A Guy Called Gerald", title="When There Is No Sun")
        discogs_titles = {
            0: ResolvedRelease(
                release_id=36907527,
                release_url="https://www.discogs.com/release/36907527",
                is_compilation=True,
                album_title="When There Is No Sun",
            )
        }
        svc = AsyncMock()
        svc.cache_service = None
        svc.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
        svc.get_release = AsyncMock(
            return_value=ReleaseMetadataResponse(
                release_id=36907527,
                title="When There Is No Sun",
                artist="Various",
                release_url="https://www.discogs.com/release/36907527",
                artwork_url="https://i.discogs.com/sun.jpg",
            )
        )
        return item, discogs_titles, svc

    @pytest.mark.asyncio
    async def test_rowless_bind_suppressed_when_allow_false(self, enable_nonlibrary_release):
        item, discogs_titles, svc = self._rowless_inputs()

        results = await fetch_artwork_for_items(
            [item],
            svc,
            discogs_titles=discogs_titles,
            allow_release_resolution_fallback=False,
        )

        # No trust-bind: the carried release is not bound by id, so the row-less
        # artwork fetch (_resolve_fallback_artwork -> get_release) never fires and
        # the floor re-search (empty) leaves the item unbound.
        svc.get_release.assert_not_awaited()
        assert results[0][1] is None

    @pytest.mark.asyncio
    async def test_rowless_bind_fires_when_allow_true(self, enable_nonlibrary_release):
        """Default path (the /lookup default): the same inputs DO trust-bind the
        carried release — the regression guard the kill-switch test is measured
        against."""
        item, discogs_titles, svc = self._rowless_inputs()

        results = await fetch_artwork_for_items([item], svc, discogs_titles=discogs_titles)

        bound = results[0][1]
        assert bound is not None
        assert bound.release_id == 36907527
        svc.get_release.assert_awaited()


class TestFetchArtworkFoundOnCompilation:
    """LML#684 (widened): a ``found_on_compilation`` in-library result carries a
    validated ``ResolvedRelease`` on the seam, and that release is preferred over
    the artist-floor re-search for artwork binding.

    Two failure modes the carried bind covers:

    - The floor re-search *rejects* every candidate — the systematic failure for
      a *non*-Various-Artists trio / collaboration credit (library row filed
      under "Bill Orcutt" vs the Discogs trio credit on "Orcutt Shelley Miller",
      release 34993109). Without the bind the row surfaces with no artwork.
    - The floor re-search *clears on the wrong release* — for a generic V/A comp
      title the floor (title+artist similarity only, never track-validated) can
      bind a *same-titled* release that does not carry the track. This is the
      prod divergence (LML#956) where "Greatest hits of the 50s & 60s"
      bound Plaza House's 13332759 instead of the validated 605487.

    The carried release was already validated by ``validate_release_for_track``
    during ``search_compilations_for_track``, so it is strictly more trustworthy
    than the floor pick and binding it costs no extra Discogs fan-out (unlike the
    ``lml_resolve_compilation_release`` lazy ``resolve_release_for_track``
    fallback). The bind is therefore independent of that flag — these tests run
    with it at its default (off).
    """

    def _trio_inputs(self):
        """An in-library row filed under one trio member, its carried (validated)
        release, and a service whose floor re-search finds nothing (so an
        unbound result leaves artwork ``None``)."""
        item = make_library_item(id=42, artist="Bill Orcutt", title="Orcutt-Shelley-Miller")
        discogs_titles = {
            42: ResolvedRelease(
                release_id=34993109,
                release_url="https://www.discogs.com/release/34993109",
                is_compilation=False,
                album_title="Orcutt Shelley Miller",
            )
        }
        svc = AsyncMock()
        svc.cache_service = None
        # Floor re-search can't clear the trio credit -> find_best_typed_match -> None.
        svc.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
        svc.get_release = AsyncMock(
            return_value=ReleaseMetadataResponse(
                release_id=34993109,
                title="Orcutt Shelley Miller",
                artist="Bill Orcutt, Chris Corsano, Sarah Louise",
                release_url="https://www.discogs.com/release/34993109",
                artwork_url="https://i.discogs.com/osm.jpg",
            )
        )
        return item, discogs_titles, svc

    @pytest.mark.asyncio
    async def test_binds_carried_release_when_floor_rejects(self):
        """found_on_compilation=True + carried release + floor rejection ->
        trust-bind the carried release's artwork (the #684 acceptance criterion)."""
        item, discogs_titles, svc = self._trio_inputs()

        results = await fetch_artwork_for_items(
            [item],
            svc,
            discogs_titles=discogs_titles,
            song="A Star Is Born",
            album="Orcutt Shelley Miller",
            found_on_compilation=True,
        )

        bound = results[0][1]
        assert bound is not None
        assert bound.release_id == 34993109
        assert bound.release_url == "https://www.discogs.com/release/34993109"
        assert bound.artwork_url == "https://i.discogs.com/osm.jpg"
        # Art comes from the carried release's own cover via _resolve_fallback_artwork.
        svc.get_release.assert_awaited()

    @pytest.mark.asyncio
    async def test_not_found_on_compilation_leaves_floor_rejection_unbound(self):
        """Scope guard: the same inputs WITHOUT found_on_compilation keep the
        pre-#684 behavior — a floor-rejected row with a carried release stays
        unbound (the flag-off floor path is unchanged for non-compilation hits)."""
        item, discogs_titles, svc = self._trio_inputs()

        results = await fetch_artwork_for_items(
            [item],
            svc,
            discogs_titles=discogs_titles,
            song="A Star Is Born",
            album="Orcutt Shelley Miller",
            found_on_compilation=False,
        )

        assert results[0][1] is None
        svc.get_release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_carried_validated_release_preferred_over_floor_match(self):
        """The carried release wins even when the floor re-search clears.

        The floor re-search is only a title+artist similarity match (never
        track-validated), so for a generic V/A comp title it can clear the floor
        on a *same-titled* release that does NOT carry the track — the prod
        divergence where "Greatest hits of the 50s & 60s" bound Plaza House's
        13332759 instead of the validated 605487. The carried release WAS
        track-validated in search_compilations_for_track, so it must win over the
        unvalidated floor pick, not merely serve as a fallback when the floor
        rejects. Binding the carried release also skips the re-search entirely.
        """
        item, discogs_titles, svc = self._trio_inputs()
        # Arm the floor with a clean, matching candidate (111) that WOULD win if
        # the re-search ran. The early trust-bind returns before the search, so
        # this candidate is deliberately never consumed — the ``assert_not_awaited``
        # below proves the carried release (34993109) pre-empts even a
        # would-succeed floor, not merely a rejecting one.
        svc.search = AsyncMock(
            return_value=DiscogsSearchResponse(
                results=[
                    make_discogs_result(
                        release_id=111,
                        album="Orcutt Shelley Miller",
                        artist="Bill Orcutt",
                        artwork_url="https://i.discogs.com/floor.jpg",
                    )
                ]
            )
        )

        results = await fetch_artwork_for_items(
            [item],
            svc,
            discogs_titles=discogs_titles,
            song="A Star Is Born",
            album="Orcutt Shelley Miller",
            found_on_compilation=True,
        )

        bound = results[0][1]
        assert bound is not None
        assert bound.release_id == 34993109
        assert bound.artwork_url == "https://i.discogs.com/osm.jpg"
        # The carried release binds before the re-search, so the floor never runs.
        svc.search.assert_not_awaited()
        svc.get_release.assert_awaited()


class TestFetchArtworkFallback:
    """Tests for artwork fallback to the artist image (LML#687: the label
    image rung was removed -- a label logo is not album art)."""

    @pytest.mark.asyncio
    async def test_falls_back_to_artist_image(self, mock_discogs_service):
        """When search returns result with no artwork, fall back to artist image."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=28138, album="Confield", artist="Autechre", artwork_url=None
                )
            ]
        )
        mock_discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield",
            artist="Autechre",
            artist_id=77,
            release_url="https://www.discogs.com/release/28138",
        )
        mock_discogs_service.get_artist_image.return_value = (
            "https://i.discogs.com/artist-photo.jpg"
        )

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].artwork_url == "https://i.discogs.com/artist-photo.jpg"
        mock_discogs_service.get_artist_image.assert_called_once_with(77)

    @pytest.mark.asyncio
    async def test_no_label_image_fallback(self, mock_discogs_service):
        """When artist image is also unavailable, artwork_url resolves to None
        rather than falling back to the label logo (LML#687) -- a label image
        is essentially never correct album art."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=28138, album="Confield", artist="Autechre", artwork_url=None
                )
            ]
        )
        mock_discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield",
            artist="Autechre",
            artist_id=77,
            label_id=233,
            release_url="https://www.discogs.com/release/28138",
        )
        mock_discogs_service.get_artist_image.return_value = None
        mock_discogs_service.get_label_image.return_value = "https://i.discogs.com/label-logo.jpg"

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].artwork_url is None
        mock_discogs_service.get_label_image.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_fallback_when_artwork_exists(self, mock_discogs_service):
        """When search returns result with artwork, no fallback calls made."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=28138,
                    album="Confield",
                    artist="Autechre",
                    artwork_url="https://i.discogs.com/cover.jpg",
                )
            ]
        )

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 1
        assert results[0][1].artwork_url == "https://i.discogs.com/cover.jpg"
        mock_discogs_service.get_release.assert_not_called()
        mock_discogs_service.get_artist_image.assert_not_called()
        mock_discogs_service.get_label_image.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_result_when_all_fallbacks_fail(self, mock_discogs_service):
        """When all fallbacks fail, result returned with artwork_url=None."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=28138, album="Confield", artist="Autechre", artwork_url=None
                )
            ]
        )
        mock_discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield",
            artist="Autechre",
            release_url="https://www.discogs.com/release/28138",
        )

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].artwork_url is None

    @pytest.mark.asyncio
    async def test_fallback_when_get_release_returns_none(self, mock_discogs_service):
        """When get_release returns None, result still returned with no artwork."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=28138, album="Confield", artist="Autechre", artwork_url=None
                )
            ]
        )
        mock_discogs_service.get_release.return_value = None

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].artwork_url is None

    @pytest.mark.asyncio
    async def test_uses_release_artwork_when_search_misses(self, mock_discogs_service):
        """When search returned no cover_image but get_release surfaces an
        artwork_url (from images[0].uri), prefer the release-level cover over
        the artist/label image fallback. The /proxy/metadata/album legacy
        two-call path already does this via populateReleaseMetadata; the
        enrichment-worker takes the single-call path, so the fallback resolver
        is the only place that can surface the cover for it."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=28138, album="Confield", artist="Autechre", artwork_url=None
                )
            ]
        )
        mock_discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield",
            artist="Autechre",
            artist_id=77,
            label_id=233,
            artwork_url="https://i.discogs.com/release-cover.jpg",
            release_url="https://www.discogs.com/release/28138",
        )

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 1
        assert results[0][1].artwork_url == "https://i.discogs.com/release-cover.jpg"
        mock_discogs_service.get_artist_image.assert_not_called()
        mock_discogs_service.get_label_image.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: _resolve_fallback_artwork never-asked re-ask (LML#1237)
# ---------------------------------------------------------------------------


class TestResolveFallbackArtworkNeverAskedReask:
    """LML#1237. ``get_release``'s LML#542 widened predicate treats a
    tracklist-bearing cache row as a hit even when ``artwork_checked_at IS
    NULL`` -- correct for `get_release` callers in general, but it means
    ``_resolve_fallback_artwork`` read that row's NULL ``artwork_url`` as
    "this release has no cover" without Discogs ever being asked. The fix is
    at this call site: it asks ``get_release`` for an artwork-authoritative
    answer via ``require_artwork_answer=True``, narrowing the predicate back
    down for exactly this caller (see ``discogs/service.py``'s
    ``TestGetRelease.test_require_artwork_answer_forces_reask_on_never_asked_tracklist_row``
    for the cache-layer half of this fix).
    """

    @pytest.mark.asyncio
    async def test_requests_artwork_authoritative_answer_by_default(self, mock_discogs_service):
        """Default call (the normal /lookup path): _resolve_fallback_artwork
        must ask get_release for an authoritative artwork answer rather than
        accepting a stale 'never checked' None."""
        mock_discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1573110,
            title="Confield",
            artist="Autechre",
            artwork_url="https://img.discogs.com/confield-cover.jpg",
            release_url="https://www.discogs.com/release/1573110",
        )

        result = await _resolve_fallback_artwork(mock_discogs_service, 1573110)

        assert result == "https://img.discogs.com/confield-cover.jpg"
        mock_discogs_service.get_release.assert_called_once_with(
            1573110, require_artwork_answer=True
        )

    @pytest.mark.asyncio
    async def test_bulk_kill_switch_suppresses_the_reask(self, mock_discogs_service):
        """LML#671/#652 bulk kill switch: the /lookup/bulk 35k-album drain
        passes allow_release_resolution_fallback=False so a never-asked slice
        of the population can't turn into a single-run stampede against the
        shared Discogs rate bucket (#879). With the switch off,
        _resolve_fallback_artwork must NOT request the live-authoritative
        answer -- it reads whatever PG already has, exactly as before this
        fix."""
        mock_discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=1573110,
            title="Confield",
            artist="Autechre",
            artwork_url=None,
            release_url="https://www.discogs.com/release/1573110",
        )

        result = await _resolve_fallback_artwork(
            mock_discogs_service, 1573110, allow_release_resolution_fallback=False
        )

        assert result is None
        mock_discogs_service.get_release.assert_called_once_with(
            1573110, require_artwork_answer=False
        )


# ---------------------------------------------------------------------------
# Tests: _resolve_fallback_artwork sentinel guard (LML#518)
# ---------------------------------------------------------------------------


class TestResolveFallbackArtworkSentinelGuard:
    """`_resolve_fallback_artwork` must reject structurally invalid release_ids
    (the synthesized `release_id=0` sentinel from LML#401, and any negative id
    that could arrive from a malformed upstream payload) before issuing the
    Discogs API call. The downstream `if not release: return None` after
    `get_release` would swallow the 404 silently — the symptom is only wasted
    API budget and Sentry noise on `/releases/0`."""

    @pytest.mark.asyncio
    async def test_release_id_zero_returns_none_without_api_call(self, mock_discogs_service):
        result = await _resolve_fallback_artwork(mock_discogs_service, 0)

        assert result is None
        mock_discogs_service.get_release.assert_not_called()

    @pytest.mark.asyncio
    async def test_negative_release_id_returns_none_without_api_call(self, mock_discogs_service):
        result = await _resolve_fallback_artwork(mock_discogs_service, -1)

        assert result is None
        mock_discogs_service.get_release.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: search_song_as_track (catalog-track-search §4.2, LML#301)
# ---------------------------------------------------------------------------


class TestSearchSongAsTrack:
    """SONG_AS_TRACK strategy: cross-reference song against Discogs, match
    releases back to library, emit matched_via TrackMatchHint per row."""

    @pytest.mark.asyncio
    async def test_no_song_returns_empty(self):
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        results, matched_via, _ = await search_song_as_track(db, None)
        assert results == []
        assert matched_via == {}
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blank_song_returns_empty(self):
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        results, matched_via, _ = await search_song_as_track(db, "")
        assert results == []
        assert matched_via == {}

    @pytest.mark.asyncio
    async def test_no_discogs_service_returns_empty(self):
        """Without a Discogs service, the strategy can't run and must no-op."""
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        results, matched_via, _ = await search_song_as_track(
            db, "vi scose poise", discogs_service=None
        )
        assert results == []
        assert matched_via == {}
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_discogs_releases_returns_empty(self):
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="vi scose poise", artist=None, releases=[], total=0
        )

        results, matched_via, _ = await search_song_as_track(
            db, "vi scose poise", discogs_service=svc
        )

        assert results == []
        assert matched_via == {}
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_library_miss_returns_empty(self):
        """Discogs returns releases but none match in library — empty result."""
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search.return_value = []
        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="unknown song",
            releases=[
                DiscogsReleaseInfo(
                    album="Some Album",
                    artist="Some Artist",
                    release_id=999,
                    release_url="https://discogs.com/release/999",
                    is_compilation=False,
                )
            ],
            total=1,
        )

        results, matched_via, _ = await search_song_as_track(
            db, "unknown song", discogs_service=svc
        )

        assert results == []
        assert matched_via == {}

    @pytest.mark.asyncio
    async def test_match_emits_track_match_hint(self):
        """Confield case: 'vi scose poise' → Autechre's Confield.

        The matched row gets a TrackMatchHint with the song as title,
        source=discogs_release, confidence at the master-cap floor (≤ 0.85
        per plan §5.2), and no per-track position (we don't fetch tracklists).
        """
        confield = make_library_item(id=60359, artist="Autechre", title="Confield")
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search.return_value = [confield]
        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="vi scose poise",
            releases=[
                DiscogsReleaseInfo(
                    album="Confield",
                    artist="Autechre",
                    release_id=8434,
                    release_url="https://discogs.com/release/8434",
                    is_compilation=False,
                )
            ],
            total=1,
        )

        results, matched_via, _ = await search_song_as_track(
            db, "vi scose poise", discogs_service=svc
        )

        assert results == [confield]
        assert 60359 in matched_via
        hints = matched_via[60359]
        assert len(hints) == 1
        hint = hints[0]
        assert hint.title == "vi scose poise"
        assert hint.source == TrackMatchSource.discogs_release
        assert hint.confidence is not None and hint.confidence <= 0.85
        assert hint.position is None

    @pytest.mark.asyncio
    async def test_compilation_match_carries_artist_credit(self):
        """For VA compilations, the hint records the per-release artist credit.

        Plan §5.1: artist_credit is the per-track artist for compilations;
        null for non-comp tracks where the release-level artist applies.
        """
        va_album = make_library_item(
            id=12345,
            artist="Various Artists",
            title="Trax Records 20th Anniversary Collection",
        )
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search.return_value = [va_album]
        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="No Way Back",
            releases=[
                DiscogsReleaseInfo(
                    album="Trax Records 20th Anniversary Collection",
                    artist="Adonis",
                    release_id=555,
                    release_url="https://discogs.com/release/555",
                    is_compilation=True,
                )
            ],
            total=1,
        )

        results, matched_via, _ = await search_song_as_track(db, "No Way Back", discogs_service=svc)

        assert results == [va_album]
        assert matched_via[12345][0].artist_credit == "Adonis"

    @pytest.mark.asyncio
    async def test_validation_failure_skips_release(self):
        """When validate_track_on_release returns False, the release is dropped.

        Discogs's release-search index returns keyword hits that don't always
        carry the song on their tracklist; validation rejects those before they
        surface as false positives in the API response.
        """
        confield = make_library_item(id=60359, artist="Autechre", title="Confield")
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search.return_value = [confield]
        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="vi scose poise",
            releases=[
                DiscogsReleaseInfo(
                    album="Confield",
                    artist="Autechre",
                    release_id=8434,
                    release_url="https://discogs.com/release/8434",
                    is_compilation=False,
                )
            ],
            total=1,
        )
        svc.validate_track_on_release.return_value = False

        results, matched_via, _ = await search_song_as_track(
            db, "vi scose poise", discogs_service=svc
        )

        assert results == []
        assert matched_via == {}
        svc.validate_track_on_release.assert_awaited_once_with(8434, "vi scose poise", "Autechre")

    @pytest.mark.asyncio
    async def test_multiple_releases_accumulate_hints_for_same_row(self):
        """Two Discogs releases pointing at the same WXYC row accumulate hints.

        A reissue/remaster pair shares the same library row but distinct
        Discogs release IDs. matched_via_by_id[id] must list one hint per
        release rather than collapsing to a single entry.
        """
        confield = make_library_item(id=60359, artist="Autechre", title="Confield")
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search.return_value = [confield]
        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="vi scose poise",
            releases=[
                DiscogsReleaseInfo(
                    album="Confield",
                    artist="Autechre",
                    release_id=8434,
                    release_url="https://discogs.com/release/8434",
                    is_compilation=False,
                ),
                DiscogsReleaseInfo(
                    album="Confield",
                    artist="Autechre",
                    release_id=999,
                    release_url="https://discogs.com/release/999",
                    is_compilation=False,
                ),
            ],
            total=2,
        )

        results, matched_via, _ = await search_song_as_track(
            db, "vi scose poise", discogs_service=svc
        )

        assert results == [confield]
        assert len(matched_via[60359]) == 2

    @pytest.mark.asyncio
    async def test_compilation_falls_back_to_various_prefix_search(self):
        """When album-only fuzzy search misses for a compilation, retry with 'Various '.

        FTS5 entries stored as "Various Artists — <title>" don't always match a
        bare album-title query; prefixing with 'Various' helps the tokenizer.
        """
        va_album = make_library_item(
            id=11111,
            artist="Various Artists",
            title="Trax Records 20th Anniversary Collection",
        )
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])

        async def search_side_effect(query, **kwargs):
            return [va_album] if query.startswith("Various ") else []

        db.search.side_effect = search_side_effect

        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="No Way Back",
            releases=[
                DiscogsReleaseInfo(
                    album="Trax Records 20th Anniversary Collection",
                    artist="Adonis",
                    release_id=222,
                    release_url="https://discogs.com/release/222",
                    is_compilation=True,
                )
            ],
            total=1,
        )

        results, matched_via, _ = await search_song_as_track(db, "No Way Back", discogs_service=svc)

        assert results == [va_album]
        assert matched_via[11111][0].artist_credit == "Adonis"
        queries = [call.kwargs["query"] for call in db.search.await_args_list]
        assert any(q.startswith("Various ") for q in queries)

    @pytest.mark.asyncio
    async def test_validate_track_calls_run_concurrently(self):
        """The per-release validate_track_on_release calls must run concurrently.

        Early-May regression: the original SONG_AS_TRACK loop awaited
        ``validate_track_on_release`` per release serially. With ~7 candidates
        per lookup at ~1.2s each, that's an 8-10s serial chain on the
        ``/api/v1/lookup`` p95/p99 hot path. This test pins the parallelization:
        with 5 candidates each sleeping 200ms, the wall time must be much closer
        to 200ms than to 1000ms (5 * 200ms).
        """
        import asyncio as _asyncio
        import time as _time

        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        releases = [
            DiscogsReleaseInfo(
                album=f"Album {i}",
                artist=f"Artist {i}",
                release_id=1000 + i,
                release_url=f"https://discogs.com/release/{1000 + i}",
                is_compilation=False,
            )
            for i in range(5)
        ]
        # All library rows must match all releases (per the
        # release_matches_library_row predicate's artist prefix match) so the
        # validate step actually fires for every release.
        items = [
            make_library_item(id=2000 + i, artist=f"Artist {i}", title=f"Album {i}")
            for i in range(5)
        ]

        async def search_side_effect(query, **kwargs):
            # Library FTS returns the row whose title matches the album query.
            for item in items:
                if (item.title or "").lower() == query.lower():
                    return [item]
            return []

        db.search.side_effect = search_side_effect

        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="t", releases=releases, total=len(releases)
        )

        async def slow_validate(release_id, track, artist):
            await _asyncio.sleep(0.2)
            return True

        svc.validate_track_on_release.side_effect = slow_validate

        start = _time.perf_counter()
        results, _matched_via, _ = await search_song_as_track(db, "t", discogs_service=svc)
        elapsed = _time.perf_counter() - start

        # Serial would be ~1.0s; concurrent should be ~0.2s. The 0.5s ceiling
        # gives headroom for scheduler jitter while still catching a serial
        # regression with comfortable margin.
        assert elapsed < 0.5, (
            f"validate_track_on_release calls ran serially "
            f"(elapsed={elapsed:.3f}s, expected near 0.2s)"
        )
        # All releases validated true, so all rows surface (capped at MAX_SEARCH_RESULTS).
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_output_order_follows_input_not_completion(self):
        """Output ``matched_items`` order must follow input order, not completion order.

        Discogs returns its release-search candidates pre-sorted by relevance,
        so the surfaced library rows must preserve that relevance ranking even
        when the underlying validate calls finish out-of-order. With releases
        A (slow validate, valid), B (fast validate, invalid), C (medium
        validate, valid), the output must be [A_row, C_row] — not [C_row]
        alone, not [C_row, A_row].
        """
        import asyncio as _asyncio

        item_a = make_library_item(id=101, artist="Artist A", title="Album A")
        item_c = make_library_item(id=103, artist="Artist C", title="Album C")
        item_b = make_library_item(id=102, artist="Artist B", title="Album B")

        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])

        async def search_side_effect(query, **kwargs):
            q = query.lower()
            if "album a" in q:
                return [item_a]
            if "album b" in q:
                return [item_b]
            if "album c" in q:
                return [item_c]
            return []

        db.search.side_effect = search_side_effect

        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="t",
            releases=[
                DiscogsReleaseInfo(
                    album="Album A",
                    artist="Artist A",
                    release_id=1,
                    release_url="https://discogs.com/release/1",
                    is_compilation=False,
                ),
                DiscogsReleaseInfo(
                    album="Album B",
                    artist="Artist B",
                    release_id=2,
                    release_url="https://discogs.com/release/2",
                    is_compilation=False,
                ),
                DiscogsReleaseInfo(
                    album="Album C",
                    artist="Artist C",
                    release_id=3,
                    release_url="https://discogs.com/release/3",
                    is_compilation=False,
                ),
            ],
            total=3,
        )

        async def validate_side_effect(release_id, track, artist):
            # A: slow + valid, B: fast + invalid (finishes first), C: medium + valid.
            if release_id == 1:
                await _asyncio.sleep(0.20)
                return True
            if release_id == 2:
                await _asyncio.sleep(0.02)
                return False
            await _asyncio.sleep(0.10)
            return True

        svc.validate_track_on_release.side_effect = validate_side_effect

        results, _matched_via, _ = await search_song_as_track(db, "t", discogs_service=svc)

        # Input order is [A, B, C]; B drops on validation; output must be [A, C].
        assert results == [item_a, item_c], (
            f"Expected input-order [A, C], got {[r.title for r in results]}. "
            "Output order must follow input (relevance) order, not completion order."
        )

    @pytest.mark.asyncio
    async def test_matched_via_hint_order_follows_input(self):
        """When one library row matches across multiple releases, the hint order
        in ``matched_via_by_id[id]`` must follow input (relevance) order — not
        the completion order of the underlying validate calls.

        Same WXYC row referenced by releases X (slow) and Y (fast), both valid:
        hints must be [X_hint, Y_hint], matching the input candidate order.
        """
        import asyncio as _asyncio

        confield = make_library_item(id=60359, artist="Autechre", title="Confield")
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search.return_value = [confield]

        # Use the compilation path so we can vary artist_credit per release
        # (the load-bearing distinguisher) while keeping a single library row.
        # For compilations, the library row's artist is the VA marker and the
        # release-level artist becomes the hint's ``artist_credit`` — that's
        # what we read back to assert input-order accumulation.
        va_album = make_library_item(id=60359, artist="Various Artists", title="Confield Comp")
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search.return_value = [va_album]

        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="vi scose poise",
            releases=[
                DiscogsReleaseInfo(
                    album="Confield Comp",
                    artist="Credit X",  # first in input
                    release_id=8434,
                    release_url="https://discogs.com/release/8434",
                    is_compilation=True,
                ),
                DiscogsReleaseInfo(
                    album="Confield Comp",
                    artist="Credit Y",  # second in input
                    release_id=999,
                    release_url="https://discogs.com/release/999",
                    is_compilation=True,
                ),
            ],
            total=2,
        )

        async def validate_side_effect(release_id, track, artist):
            if release_id == 8434:
                await _asyncio.sleep(0.10)  # X finishes second
            else:
                await _asyncio.sleep(0.01)  # Y finishes first
            return True

        svc.validate_track_on_release.side_effect = validate_side_effect

        results, matched_via, _ = await search_song_as_track(
            db, "vi scose poise", discogs_service=svc
        )

        assert results == [va_album]
        hints = matched_via[60359]
        assert len(hints) == 2
        # Input order is [X, Y]; Y completes first but the post-gather walk
        # must preserve input order, so the hint sequence is [X-credit, Y-credit].
        assert [h.artist_credit for h in hints] == ["Credit X", "Credit Y"]

    @pytest.mark.asyncio
    async def test_validate_concurrency_is_bounded(self):
        """No more than the configured cap of validate calls may be in-flight
        at once.

        Counters the naive ``asyncio.gather(*all_releases)`` shape, which can
        explode parallelism when Discogs returns 50+ candidates. The
        orchestrator-local semaphore bounds in-flight validate calls; the
        global Discogs rate limiter sits underneath.
        """
        import asyncio as _asyncio

        n_candidates = 20
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])

        # Each release maps to a distinct library row that prefix-matches it,
        # so validate fires for every one.
        items = [
            make_library_item(id=5000 + i, artist=f"Artist {i}", title=f"Album {i}")
            for i in range(n_candidates)
        ]
        releases = [
            DiscogsReleaseInfo(
                album=f"Album {i}",
                artist=f"Artist {i}",
                release_id=10000 + i,
                release_url=f"https://discogs.com/release/{10000 + i}",
                is_compilation=False,
            )
            for i in range(n_candidates)
        ]

        async def search_side_effect(query, **kwargs):
            for item in items:
                if (item.title or "").lower() == query.lower():
                    return [item]
            return []

        db.search.side_effect = search_side_effect

        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="t", releases=releases, total=n_candidates
        )

        in_flight = 0
        peak_in_flight = 0
        lock = _asyncio.Lock()

        async def validate_side_effect(release_id, track, artist):
            nonlocal in_flight, peak_in_flight
            async with lock:
                in_flight += 1
                if in_flight > peak_in_flight:
                    peak_in_flight = in_flight
            try:
                await _asyncio.sleep(0.05)
                return True
            finally:
                async with lock:
                    in_flight -= 1

        svc.validate_track_on_release.side_effect = validate_side_effect

        # The per-request fan-out cap is the chunk size passed to
        # ``_chunked_gather``, which the three call sites pin to
        # ``MAX_SEARCH_RESULTS``. Pin the invariant against the same constant
        # so the test still tracks if the value is tuned later.
        from lookup.matching import MAX_SEARCH_RESULTS as _CAP

        await search_song_as_track(db, "t", discogs_service=svc)

        assert peak_in_flight <= _CAP, (
            f"validate_track_on_release ran with {peak_in_flight} in-flight; "
            f"the orchestrator-local cap is {_CAP}."
        )
        # Sanity: parallelism > 1 (otherwise we accidentally serialized).
        assert peak_in_flight > 1, (
            f"Peak in-flight was {peak_in_flight}; the validate calls did not "
            "run concurrently at all."
        )

    @pytest.mark.asyncio
    async def test_early_exits_after_max_results_accumulated(self):
        """No further ``validate_track_on_release`` calls fire once
        ``MAX_SEARCH_RESULTS`` matches accumulate.

        LML#536: the pre-PR (#534) serial loop short-circuited as soon as
        ``MAX_SEARCH_RESULTS`` matches landed; the ``asyncio.gather`` conversion
        scheduled every candidate's validate call regardless. With ~15
        high-fanout candidates that's ~10 wasted Discogs round-trips per
        request — each one a ``/releases/<id>`` API hit (Sentry p95 5.87s)
        when the PG cache write path is degraded
        (Sentry LIBRARY-METADATA-LOOKUP-9).

        Pin the invariant: with 15 candidates that would all validate true,
        the function must surface exactly ``MAX_SEARCH_RESULTS`` results and
        fire at most ``MAX_SEARCH_RESULTS`` validate calls.
        """
        from lookup.matching import MAX_SEARCH_RESULTS

        n_candidates = MAX_SEARCH_RESULTS * 3
        items = [
            make_library_item(id=5000 + i, artist=f"Artist {i}", title=f"Album {i}")
            for i in range(n_candidates)
        ]
        releases = [
            DiscogsReleaseInfo(
                album=f"Album {i}",
                artist=f"Artist {i}",
                release_id=10000 + i,
                release_url=f"https://discogs.com/release/{10000 + i}",
                is_compilation=False,
            )
            for i in range(n_candidates)
        ]

        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])

        async def search_side_effect(query, **kwargs):
            for item in items:
                if (item.title or "").lower() == query.lower():
                    return [item]
            return []

        db.search.side_effect = search_side_effect

        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="t", releases=releases, total=n_candidates
        )
        svc.validate_track_on_release.return_value = True

        results, _matched_via, _ = await search_song_as_track(db, "t", discogs_service=svc)

        assert len(results) == MAX_SEARCH_RESULTS
        n_validate_calls = svc.validate_track_on_release.await_count
        assert n_validate_calls <= MAX_SEARCH_RESULTS, (
            f"Expected early-exit after at most {MAX_SEARCH_RESULTS} validate calls, "
            f"got {n_validate_calls} (LML#536: gather conversion lost early exit)."
        )


class TestSearchSongAsTrackQueryCoverage1225:
    """End-to-end LML#1225 regression: a REAL ``DiscogsService`` with only its
    network-touching ``get_release``/``search_releases_by_track`` methods
    mocked, so ``validate_track_on_release`` runs for real and exercises the
    actual ``scan_tracklist_for_match`` kernel (not an ``AsyncMock`` stand-in
    for the whole validation step, the way most of ``TestSearchSongAsTrack``
    above does). This is the shape of the real production bug: a SONG_AS_TRACK
    library HIT that should never have validated.

    ``allow_release_resolution_fallback=False`` turns off the LML#628 row-less
    carry-through, which would otherwise retry resolution through a separate
    path and entangle these assertions with that feature's own coverage. Note
    that flag is ON in production, so a total rejection there costs additional
    Discogs fetches through ``_resolve_nonlibrary_release`` -- that cost is
    LML#628's to characterize, not this regression's.
    """

    @pytest.mark.parametrize(
        "song, library_artist, library_title, release_id, track_title, expect_hit",
        [
            pytest.param(
                "Space Lizzard Battle Star hell cat",
                "Population One",
                "Theater Of A Confused Mind",
                6194406,
                "Battle For Space",
                False,
                id="fuzzy_arm_repro",
            ),
            pytest.param(
                "Purple Refrigerator Symphony No 9",
                "Ludwig van Beethoven",
                "Nine Symphonies",
                1234567,
                "Symphony No. 9",
                False,
                id="substring_arm_repro",
            ),
            pytest.param(
                "Battle For Space",
                "Population One",
                "Theater Of A Confused Mind",
                6194406,
                "Battle For Space",
                True,
                id="clean_query_recall_control",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_query_coverage_gate_end_to_end(
        self, song, library_artist, library_title, release_id, track_title, expect_hit
    ):
        """Both production repros (2026-08-18), each entering through a
        different title-gate arm, plus a no-regression control.

        ``fuzzy_arm_repro``: token_set_ratio scores 85.71 against "Battle For
        Space", clearing the 85 floor because it structurally ignores the four
        query tokens that match nothing. ``substring_arm_repro``: the
        normalized title "symphony no 9" is a literal substring of the query,
        so it enters via the substring arm and never reaches the fuzzy
        fallback at all. ``clean_query_recall_control``: the SAME release as
        repro 1, queried with the track's real un-padded title, must still
        surface the library row -- the fix narrows precision without costing
        recall on a genuine match.
        """
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])
        db.search.return_value = [make_library_item(artist=library_artist, title=library_title)]

        release_url = f"https://discogs.com/release/{release_id}"
        release = ReleaseMetadataResponse(
            release_id=release_id,
            title=library_title,
            artist=library_artist,
            release_url=release_url,
            tracklist=[TrackItem(position="B2", title=track_title)],
        )
        svc = DiscogsService(token="unused-get_release-is-mocked")
        svc.search_releases_by_track = AsyncMock(
            return_value=DiscogsTrackReleasesResponse(
                track=song,
                releases=[
                    DiscogsReleaseInfo(
                        album=library_title,
                        artist=library_artist,
                        release_id=release_id,
                        release_url=release_url,
                        is_compilation=False,
                    )
                ],
                total=1,
            )
        )
        with patch.object(svc, "get_release", new_callable=AsyncMock, return_value=release):
            results, matched_via, _ = await search_song_as_track(
                db,
                song,
                discogs_service=svc,
                allow_release_resolution_fallback=False,
            )

        if expect_hit:
            assert len(results) == 1
            assert results[0].artist == library_artist
            assert matched_via
        else:
            assert results == []
            assert matched_via == {}


class TestLogReleaseResolutionBind:
    """LML#604 telemetry: every time the lazy release-resolution fallback fires
    it records whether it bound, so adoption + cost (fired-vs-bound rate) are
    observable in Railway logs and the Sentry trace without a wire field."""

    def test_logs_payload_on_bind(self):
        from lookup.artist_resolution import _log_release_resolution_bind

        with patch("lookup.artist_resolution.logger") as mock_logger:
            _log_release_resolution_bind(
                song="Message to Black Youth",
                artist="A Guy Called Gerald",
                album="When There Is No Sun",
                bound=True,
                release_id=36907527,
            )

        mock_logger.info.assert_called_once()
        fmt, payload = mock_logger.info.call_args.args
        assert fmt == "release_resolution_bind %s"
        assert payload["bound"] is True
        assert payload["release_id"] == 36907527
        assert payload["song"] == "Message to Black Youth"
        assert payload["artist"] == "A Guy Called Gerald"

    def test_records_accumulating_breadcrumb_not_overwriting_set_data(self):
        """Per-item event → an accumulating Sentry breadcrumb (like
        _log_track_validation), NOT transaction.set_data with a fixed key, which
        would last-write-win across multiple items binding in one request."""
        with patch("lookup.artist_resolution.sentry_sdk.add_breadcrumb") as mock_breadcrumb:
            from lookup.artist_resolution import _log_release_resolution_bind

            _log_release_resolution_bind(
                song="Message to Black Youth",
                artist="A Guy Called Gerald",
                album="When There Is No Sun",
                bound=True,
                release_id=36907527,
            )

        mock_breadcrumb.assert_called_once()
        assert mock_breadcrumb.call_args.kwargs["category"] == "release_resolution_bind"
        assert mock_breadcrumb.call_args.kwargs["data"]["release_id"] == 36907527

    def test_swallows_sentry_failure(self):
        from lookup.artist_resolution import _log_release_resolution_bind

        with patch(
            "lookup.artist_resolution.sentry_sdk.add_breadcrumb",
            side_effect=RuntimeError("boom"),
        ):
            # Observability must never break /lookup — no exception escapes.
            _log_release_resolution_bind(
                song="s", artist="a", album=None, bound=False, release_id=None
            )


class TestSearchCompilationsCarriedTitleRank:
    """LML#604 deferred finding #2: the carried path (TRACK_ON_COMPILATION) must
    title-rank the release it binds per library item — the same ranking the lazy
    fallback applies — so the two binding paths agree on which release wins. The
    title-rank is gated by lml_resolve_compilation_release; flag-off keeps the
    pre-PR2 first-seen behavior.

    Setup: one library row matched by two pressings of the same comp with equal
    titles but differing release ids. First-seen (Wave A order) would bind the
    higher id; title-rank resolves the title tie by the stable lowest id.
    """

    @pytest.fixture
    def enable_compilation_release(self, monkeypatch):
        monkeypatch.setenv("LML_RESOLVE_COMPILATION_RELEASE", "true")
        from config.settings import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def _setup(self):
        item = make_library_item(
            id=500, artist="Various Artists - Rock - D", title="When There Is No Sun"
        )
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[item])
        db.search = AsyncMock(return_value=[])
        # Wave A: two pressings, equal title, id=99 first (first-seen), id=10 second.
        releases = [
            ReleaseInfo(
                album="When There Is No Sun",
                artist="Various",
                release_id=99,
                release_url="https://www.discogs.com/release/99",
                is_compilation=True,
            ),
            ReleaseInfo(
                album="When There Is No Sun",
                artist="Various",
                release_id=10,
                release_url="https://www.discogs.com/release/10",
                is_compilation=True,
            ),
        ]
        svc = AsyncMock()
        svc.cache_service = None

        async def _track_releases(track, artist=None, artist_as_keyword=False, **_):
            return TrackReleasesResponse(
                track=track,
                artist=artist,
                releases=[] if artist_as_keyword else list(releases),
                total=0 if artist_as_keyword else len(releases),
            )

        svc.search_releases_by_track = AsyncMock(side_effect=_track_releases)
        svc.validate_track_on_release = AsyncMock(return_value=True)
        parsed = ParsedRequest(
            artist="A Guy Called Gerald",
            song="Message to Black Youth",
            raw_message="message to black youth a guy called gerald",
        )
        return db, svc, parsed, item

    @pytest.mark.asyncio
    async def test_title_ranks_when_flag_on(self, enable_compilation_release):
        db, svc, parsed, item = self._setup()
        with patch(
            "lookup.strategies.track_on_compilation.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            _results, titles = await search_compilations_for_track(db, parsed, discogs_service=svc)

        # Title tie → stable lowest release id, NOT first-seen (99).
        assert titles[item.id].release_id == 10

    @pytest.mark.asyncio
    async def test_first_seen_when_flag_off(self):
        from config.settings import get_settings

        get_settings.cache_clear()  # default-off
        db, svc, parsed, item = self._setup()
        with patch(
            "lookup.strategies.track_on_compilation.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            _results, titles = await search_compilations_for_track(db, parsed, discogs_service=svc)

        # Pre-PR2: first-seen (Wave A order) wins.
        assert titles[item.id].release_id == 99


class TestSearchCompilationsEarlyExit:
    """LML#536: the gather conversion lost the pre-PR early-exit. With ~15
    high-fanout candidates, every ``process_release`` task ran and validated
    against Discogs even after ``MAX_SEARCH_RESULTS`` matches had already
    landed.

    Pins the invariant: once ``MAX_SEARCH_RESULTS`` matches accumulate, no
    further ``validate_track_on_release`` calls fire.
    """

    @pytest.mark.asyncio
    async def test_main_gather_early_exits_after_max_results(self):
        """Wave A returns 15 candidates that all validate true; the post-gather
        truncation must short-circuit dispatch so only ~``MAX_SEARCH_RESULTS``
        validate calls fire.

        With the PG cache write path degraded (Sentry
        LIBRARY-METADATA-LOOKUP-9) each wasted validate falls through to the
        live ``/releases/<id>`` endpoint (p95 5.87s on that span), so the
        cost lever isn't just rate-limiter pressure — it's wall time and
        Discogs quota.
        """
        from lookup.matching import MAX_SEARCH_RESULTS

        n_candidates = MAX_SEARCH_RESULTS * 3
        items = [
            make_library_item(
                id=20000 + i,
                artist="Vivien Goldman",
                title=f"Album {i}",
            )
            for i in range(n_candidates)
        ]
        releases = [
            DiscogsReleaseInfo(
                album=f"Album {i}",
                artist="Vivien Goldman",
                release_id=30000 + i,
                release_url=f"https://www.discogs.com/release/{30000 + i}",
                is_compilation=False,
            )
            for i in range(n_candidates)
        ]

        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])

        async def _search(query, limit=None, **_):
            q = query.lower()
            for item in items:
                if (item.title or "").lower() == q:
                    return [item]
            return []

        db.search = AsyncMock(side_effect=_search)

        svc = AsyncMock()
        svc.cache_service = None

        async def _track_releases(track, artist=None, artist_as_keyword=False, **_):
            return DiscogsTrackReleasesResponse(
                track=track,
                artist=artist,
                releases=[] if artist_as_keyword else list(releases),
                total=0 if artist_as_keyword else len(releases),
            )

        svc.search_releases_by_track = AsyncMock(side_effect=_track_releases)
        svc.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="Vivien Goldman",
            song="Launderette",
            raw_message="launderette by vivien goldman",
        )

        with patch(
            "lookup.strategies.track_on_compilation.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _titles = await search_compilations_for_track(db, parsed, discogs_service=svc)

        assert len(results) == MAX_SEARCH_RESULTS
        n_validate_calls = svc.validate_track_on_release.await_count
        assert n_validate_calls <= MAX_SEARCH_RESULTS, (
            f"Expected early-exit after at most {MAX_SEARCH_RESULTS} validate calls, "
            f"got {n_validate_calls} (LML#536: gather conversion lost early exit "
            "in search_compilations_for_track)."
        )


@dataclass(frozen=True)
class _StubStrategy:
    """Minimal :class:`Strategy` for runner-level tests in ``TestApiCallCap``.

    Carries a name (for the strategies_tried ledger) and an attempt coroutine;
    ``should_attempt`` is hard-wired to True so each test controls its own
    cascade order via the strategies list.
    """

    name: object  # SearchStrategyType — kept Any to avoid the lazy import.
    attempt_func: object

    def should_attempt(self, _parsed, _state, _raw):
        return True

    async def attempt(self, parsed, state, raw):
        return await self.attempt_func(parsed, state, raw)  # type: ignore[operator]


def _fire_cap_via_recorder():
    """Drive the production cap-fire site so tests exercise both
    ``record_search_api_call_cap_fired`` paths (telemetry counter + runner
    ContextVar bump) without duplicating the helper's body."""
    from lookup.concurrency import _record_search_api_call_cap_fired

    _record_search_api_call_cap_fired(cap=3, spent=5, items_remaining=10, items_total=15)


class TestSearchByKeyword:
    """Tests for ``track_on_compilation._search_by_keyword`` (LML#1257)."""

    @pytest.mark.asyncio
    async def test_short_underscore_artist_keeps_scoping_the_keyword_query(self, mock_library_db):
        """LML#1257: this site takes the *query-token* fidelity, which leaves
        ``_`` alone -- pinning why it was NOT consolidated onto
        ``fold_punctuation_for_comparison``.

        ``sig_artist`` applies a ``len(w) > 3`` floor *after* the fold, so
        folding ``_`` here deletes short names outright rather than splitting
        them usefully. "Ras_G" -- real catalog row 66277, "The gospel of the
        God Spell" -- folds to ``["ras","g"]``, neither fragment clearing the
        floor, so the artist would contribute ZERO of its two ``query_words``
        slots and the keyword query would lose its artist scoping entirely.

        Nothing is gained by folding: ``db.search`` already folds ``_``
        itself, and queried against the real catalog "ras_g" and "ras g"
        return identical result sets (measured 2026-08-22). So the fold would
        be pure loss here, which is why this site keeps the unfolded form.
        """
        from lookup.strategies.track_on_compilation import _search_by_keyword

        item = make_library_item(id=66277, artist="Ras_G", title="The gospel of the God Spell")

        async def search(query, limit=None, **_):
            # The artist term must still be present and unfolded. Under the
            # comparison fold this query would have been "gospel spell" --
            # artist-unscoped -- and this mock would return nothing.
            if query == "ras_g gospel spell":
                return [item]
            return []

        mock_library_db.search = AsyncMock(side_effect=search)

        parsed = ParsedRequest(
            artist="Ras_G",
            song="Gospel Spell",
            raw_message="Ras_G - Gospel Spell",
            is_request=True,
            message_type=MessageType.REQUEST,
        )

        results = await _search_by_keyword(mock_library_db, "Ras_G", parsed)

        assert len(results) == 1
        assert results[0].id == 66277


_PARSED_AB = ParsedRequest(
    artist="Stereolab", song="Aluminum Tunes", raw_message="Stereolab - Aluminum Tunes"
)
"""Shared parsed request for the cap-fire integration tests. None of these
tests inspects the parsed fields — the stub strategies just thread it
through — so a single shared instance keeps boilerplate down. Uses a WXYC-
representative artist per CLAUDE.md fixture convention."""


class TestApiCallCap:
    """LML#543: per-invocation API-call cap on the orchestrator fallback cascade.

    When ``search_releases_by_track`` returns 0 canonical results for a real
    lookup, the orchestrator's chunked validation tail used to burn 15-24
    Discogs API calls per failing lookup (16-17s wall time). The cap
    short-circuits ``_chunked_gather`` between chunks once the per-invocation
    delta against ``stats["api_calls"]`` crosses ``LML_SEARCH_MAX_API_CALLS``.

    Per-invocation (not request-wide) because the cap must not (a) starve
    later items in a bulk batch that shares one cache_stats dict, nor (b)
    silently kill the LML#319/#237 album-title fallback whose ``_chunked_gather``
    follows the main loop's in ``search_compilations_for_track``. The runner's
    cap-fire propagation rides a per-task ContextVar
    (:data:`core.search._cap_fire_count_var`) so concurrent bulk items don't
    poison each other through the shared cache_stats dict.
    """

    @pytest.fixture(autouse=True)
    def _seed_cache_stats(self):
        """Initialise cache_stats with the LML#543 key seeded so PostHog
        payload shapes are stable when the counter is read at request end.
        The recorder ``record(key)`` always increments regardless of seeding;
        seeding only fixes the *shape* of the payload."""
        from wxyc_fastapi.observability import init_cache_stats

        from core.search import SEARCH_API_CALL_CAP_FIRED_STAT_KEY

        init_cache_stats(extra_keys=(SEARCH_API_CALL_CAP_FIRED_STAT_KEY,))

    @pytest.mark.asyncio
    async def test_chunked_gather_bails_after_api_call_cap(self, monkeypatch):
        """Cap fires after exactly one chunk dispatches when each validate
        bumps the counter — pins both the upper bound (≤ chunk_size) and the
        lower bound (chunk 1 fully ran)."""
        from wxyc_fastapi.observability import (
            get_cache_stats_recorder,
            init_cache_stats,
        )

        from lookup.matching import MAX_SEARCH_RESULTS

        monkeypatch.setenv("LML_SEARCH_MAX_API_CALLS", "3")
        init_cache_stats()

        n_candidates = MAX_SEARCH_RESULTS * 3
        items = [
            make_library_item(id=7000 + i, artist=f"Artist {i}", title=f"Album {i}")
            for i in range(n_candidates)
        ]
        releases = [
            DiscogsReleaseInfo(
                album=f"Album {i}",
                artist=f"Artist {i}",
                release_id=40000 + i,
                release_url=f"https://discogs.com/release/{40000 + i}",
                is_compilation=False,
            )
            for i in range(n_candidates)
        ]

        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])

        async def _search(query, limit=None, **_):
            q = query.lower()
            for item in items:
                if (item.title or "").lower() == q:
                    return [item]
            return []

        db.search = AsyncMock(side_effect=_search)

        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="t", releases=releases, total=n_candidates
        )

        async def _validate(*_args, **_kwargs):
            get_cache_stats_recorder().record_api_call()
            return False

        svc.validate_track_on_release.side_effect = _validate

        await search_song_as_track(db, "t", discogs_service=svc)

        n_validate_calls = svc.validate_track_on_release.await_count
        # Strict equality: chunk 1 fully ran (5 validates → spent=5 ≥ cap=3),
        # chunk 2 gated out. A regression that runs only one item per chunk
        # would still pass `<= MAX_SEARCH_RESULTS` but fails this.
        assert n_validate_calls == MAX_SEARCH_RESULTS, (
            f"Expected exactly {MAX_SEARCH_RESULTS} validate calls (chunk 1 "
            f"fully ran, chunk 2 cap-gated), got {n_validate_calls}. LML#543."
        )

    @pytest.mark.asyncio
    async def test_cap_is_per_invocation_not_request_wide(self, monkeypatch):
        """Two sequential ``_chunked_gather`` invocations in the same request
        each get the full cap — the cap is baseline-relative, not request-wide.

        Without this property, the LML#319/#237 album-title fallback (the
        second ``_chunked_gather`` in ``search_compilations_for_track``) is
        silently disabled once the main loop has burned its budget. This test
        bypasses the orchestrator strategies and exercises ``_chunked_gather``
        directly so the invariant is pinned regardless of caller shape.
        """
        from wxyc_fastapi.observability import (
            get_cache_stats_recorder,
            init_cache_stats,
        )

        from lookup.concurrency import _chunked_gather
        from lookup.matching import MAX_SEARCH_RESULTS

        monkeypatch.setenv("LML_SEARCH_MAX_API_CALLS", "3")
        init_cache_stats()

        async def _worker(_item):
            get_cache_stats_recorder().record_api_call()
            return None

        # First invocation: 15 items, chunks of MAX_SEARCH_RESULTS (5). Cap 3
        # bails after chunk 1 → 5 worker calls.
        count_a = 0
        async for _ in _chunked_gather(list(range(15)), _worker, MAX_SEARCH_RESULTS):
            count_a += 1
        assert count_a == MAX_SEARCH_RESULTS

        # Second invocation against the same request: must NOT inherit the
        # first invocation's spending. With baseline-relative cap, the second
        # invocation captures a fresh baseline and runs its own chunk 1.
        count_b = 0
        async for _ in _chunked_gather(list(range(15)), _worker, MAX_SEARCH_RESULTS):
            count_b += 1
        assert count_b == MAX_SEARCH_RESULTS, (
            f"Second _chunked_gather inherited the first's spent counter — "
            f"got {count_b} dispatches, expected {MAX_SEARCH_RESULTS}. "
            f"Album-title fallback bypass (LML#543 review)."
        )

    @pytest.mark.asyncio
    async def test_cap_fire_propagates_to_state_timed_out_via_runner(self):
        """Drive the runner end-to-end: a strategy whose attempt fires the cap
        must cause ``execute_search_pipeline`` to set ``state.timed_out=True``
        and short-circuit subsequent strategies."""
        from core.search import Outcome, SearchStrategyType, execute_search_pipeline

        second_strategy_ran = False

        async def _fire(_p, _s, _r):
            _fire_cap_via_recorder()
            return Outcome.empty()

        async def _second(_p, _s, _r):
            nonlocal second_strategy_ran
            second_strategy_ran = True
            return Outcome.empty()

        state = await execute_search_pipeline(
            _PARSED_AB,
            _PARSED_AB.raw_message,
            [
                _StubStrategy(SearchStrategyType.SONG_AS_TRACK, _fire),
                _StubStrategy(SearchStrategyType.KEYWORD_MATCH, _second),
            ],
        )

        assert state.timed_out is True
        assert second_strategy_ran is False, (
            "Cap-fire must short-circuit the cascade — strategy 2 should not run."
        )

    @pytest.mark.asyncio
    async def test_cap_fire_does_not_override_natural_completion(self):
        """When a strategy fires the cap AND surfaced a confirmed song match
        (``Outcome.found``, which clears ``song_not_found``), the natural-
        completion break wins — ``state.timed_out`` stays ``False`` so
        ``LookupResponse.timeout`` doesn't falsely flag a successful response."""
        from core.search import Outcome, SearchStrategyType, execute_search_pipeline

        async def _succeed_and_fire(_p, _s, _r):
            _fire_cap_via_recorder()
            return Outcome.found(
                [make_library_item(id=9000, artist="Stereolab", title="Aluminum Tunes")]
            )

        state = await execute_search_pipeline(
            _PARSED_AB,
            _PARSED_AB.raw_message,
            [_StubStrategy(SearchStrategyType.SONG_AS_TRACK, _succeed_and_fire)],
        )

        assert state.results
        assert state.timed_out is False

    @pytest.mark.asyncio
    async def test_cap_fire_propagates_under_artist_fallback_shape(self):
        """``Outcome.artist_fallback(items)`` sets both ``state.results`` and
        ``state.song_not_found=True``. The natural-completion break (which
        requires ``not state.song_not_found``) does NOT fire — so a cap-fire
        in this leg must still propagate. Mirrors LML#543's intent: 'we
        already spent the budget; the next strategy won't get cheaper.'"""
        from core.search import Outcome, SearchStrategyType, execute_search_pipeline

        followup_ran = False

        async def _artist_fallback_and_fire(_p, _s, _r):
            _fire_cap_via_recorder()
            return Outcome.artist_fallback(
                [make_library_item(id=9001, artist="Jessica Pratt", title="On Your Own Love Again")]
            )

        async def _followup(_p, _s, _r):
            nonlocal followup_ran
            followup_ran = True
            return Outcome.empty()

        state = await execute_search_pipeline(
            _PARSED_AB,
            _PARSED_AB.raw_message,
            [
                _StubStrategy(SearchStrategyType.ARTIST_PLUS_ALBUM, _artist_fallback_and_fire),
                _StubStrategy(SearchStrategyType.TRACK_ON_COMPILATION, _followup),
            ],
        )

        # Artist-fallback items survive; cap-fire short-circuits the cascade.
        assert state.results
        assert state.song_not_found is True
        assert state.timed_out is True
        assert followup_ran is False, (
            "Cap-fire in artist-fallback leg must short-circuit subsequent strategies."
        )

    @pytest.mark.asyncio
    async def test_cap_fire_does_not_leak_across_concurrent_bulk_items(self):
        """The bulk endpoint runs items CONCURRENTLY via ``asyncio.gather`` —
        the iter-2 sequential-only test missed this. Drive two pipelines in
        flight at the same time: only the one whose strategy fires the cap
        should see ``state.timed_out=True``.

        This pins the per-task ContextVar isolation
        (:data:`core.search._cap_fire_count_var`). A sticky-flag or
        shared-counter design would race-poison the sibling here."""
        import asyncio as _asyncio

        from core.search import Outcome, SearchStrategyType, execute_search_pipeline

        # Two items take turns at await points: B enters its attempt and
        # signals; A waits for that signal before firing the cap, ensuring both
        # are live in their respective tasks when A's increment happens. A
        # `wait_for(timeout=2.0)` guards against a regression that drops B's
        # `.set()` and would otherwise hang the suite.
        item_b_started = _asyncio.Event()
        item_a_fired = _asyncio.Event()

        async def _attempt_a(_p, _s, _r):
            await _asyncio.wait_for(item_b_started.wait(), timeout=2.0)
            _fire_cap_via_recorder()
            item_a_fired.set()
            return Outcome.empty()

        async def _attempt_b(_p, _s, _r):
            item_b_started.set()
            # Wait for A's fire so the test pins the property 'A's increment
            # is invisible to B' rather than the wall-clock 'A finished first'.
            await _asyncio.wait_for(item_a_fired.wait(), timeout=2.0)
            return Outcome.empty()

        state_a, state_b = await _asyncio.gather(
            execute_search_pipeline(
                _PARSED_AB,
                _PARSED_AB.raw_message,
                [_StubStrategy(SearchStrategyType.SONG_AS_TRACK, _attempt_a)],
            ),
            execute_search_pipeline(
                _PARSED_AB,
                _PARSED_AB.raw_message,
                [_StubStrategy(SearchStrategyType.KEYWORD_MATCH, _attempt_b)],
            ),
        )

        assert state_a.timed_out is True, "Item A fired the cap — must propagate."
        assert state_b.timed_out is False, (
            "Item B never fired the cap but inherited A's signal — concurrent "
            "bulk regression (iter-3 review)."
        )
