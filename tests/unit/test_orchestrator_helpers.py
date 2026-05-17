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

from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import (
    DiscogsSearchRequest,
    DiscogsSearchResponse,
    ReleaseMetadataResponse,
)
from generated.api_models import (
    DiscogsReleaseInfo,
    DiscogsTrackReleasesResponse,
    TrackMatchSource,
)
from lookup.orchestrator import (
    _log_track_validation,
    artist_matches_item,
    build_context_message,
    fetch_artwork_for_items,
    filter_results_by_artist,
    filter_results_by_track_validation,
    find_library_albums_with_cached_track,
    resolve_albums_for_track,
    search_library_with_fallback,
    search_song_as_track,
    search_with_alternative_interpretation,
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
        """Falls back to artist-only when Discogs found no albums (empty list)."""
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
            album="Unknown Album",
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

        results, _ = await search_with_alternative_interpretation(
            mock_library_db, "Amps for Christ", "Edward"
        )
        assert len(results) == 1
        assert results[0].artist == "Amps for Christ"

    @pytest.mark.asyncio
    async def test_deduplicates_results(self, mock_library_db):
        item = make_library_item(id=1, artist="Artist A", title="Album 1")
        mock_library_db.search.side_effect = [[item], [item]]

        results, _ = await search_with_alternative_interpretation(
            mock_library_db, "Artist A", "Something"
        )
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_matches(self, mock_library_db):
        mock_library_db.search.side_effect = [
            [make_library_item(id=1, artist="Wrong Artist", title="Album")],
            [make_library_item(id=2, artist="Also Wrong", title="Another")],
        ]

        results, _ = await search_with_alternative_interpretation(
            mock_library_db, "Nonexistent", "Unknown"
        )
        assert len(results) == 0


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

        result = await find_library_albums_with_cached_track(
            mock_library_db,
            "Bucky Skank",
            "Lee 'Scratch' Perry",
            mock_discogs_service,
        )

        assert len(result) == 1
        assert result[0].id == 12682

    @pytest.mark.asyncio
    async def test_returns_empty_without_discogs_service(self, mock_library_db):
        result = await find_library_albums_with_cached_track(
            mock_library_db, "Song", "Artist", None
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_without_cache_service(self, mock_library_db, mock_discogs_service):
        """No PG cache attached → helper is a no-op (we don't fall back to the API)."""
        mock_discogs_service.cache_service = None
        result = await find_library_albums_with_cached_track(
            mock_library_db, "Song", "Artist", mock_discogs_service
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_song_or_artist_missing(
        self, mock_library_db, mock_discogs_service
    ):
        mock_discogs_service.cache_service = AsyncMock()
        assert (
            await find_library_albums_with_cached_track(
                mock_library_db, None, "Artist", mock_discogs_service
            )
            == []
        )
        assert (
            await find_library_albums_with_cached_track(
                mock_library_db, "Song", None, mock_discogs_service
            )
            == []
        )
        # Cache should never be consulted when inputs are insufficient
        mock_discogs_service.cache_service.search_releases_by_track.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_empty_when_cache_has_no_matching_releases(
        self, mock_library_db, mock_discogs_service
    ):
        mock_discogs_service.cache_service = AsyncMock()
        mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(return_value=[])
        result = await find_library_albums_with_cached_track(
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

        result = await find_library_albums_with_cached_track(
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

        result = await find_library_albums_with_cached_track(
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

        result = await find_library_albums_with_cached_track(
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
        result = await find_library_albums_with_cached_track(
            mock_library_db,
            "Song",
            "Artist",
            mock_discogs_service,
        )
        assert result == []


# ---------------------------------------------------------------------------
# Tests: fetch_artwork_for_items
# ---------------------------------------------------------------------------


class TestFetchArtworkForItems:
    """Tests for parallel artwork fetching."""

    @pytest.mark.asyncio
    async def test_fetches_artwork_for_each_item(self, mock_discogs_service):
        items = [
            make_library_item(id=1, artist="Queen", title="A Night at the Opera"),
            make_library_item(id=2, artist="Queen", title="The Game"),
        ]

        artwork = make_discogs_result(
            release_id=12345,
            album="A Night at the Opera",
            artist="Queen",
            artwork_url="https://example.com/cover.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[artwork])

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 2
        # Each result is a (LibraryItem, DiscogsSearchResult | None) tuple
        assert results[0][0].id == 1
        assert results[0][1] is not None

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
        """For compilations, use the Discogs album title (not library title) for artwork."""
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

        discogs_titles = {20: "Disco Not Disco (Post Punk, Electro & Leftfield Disco Classics)"}
        results = await fetch_artwork_for_items(
            items=[item], discogs_service=mock_discogs_service, discogs_titles=discogs_titles
        )

        assert len(results) == 1
        # Should have looked up with the Discogs title, not the library title
        call_args = mock_discogs_service.search.call_args[0][0]
        assert isinstance(call_args, DiscogsSearchRequest)
        assert "Disco Not Disco" in call_args.album

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

        artwork = make_discogs_result(release_id=12345)
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[artwork])

        await fetch_artwork_for_items([item], mock_discogs_service)

        call_args = mock_discogs_service.search.call_args[0][0]
        assert call_args.label is None


class TestFetchArtworkFallback:
    """Tests for artwork fallback to artist/label images."""

    @pytest.mark.asyncio
    async def test_falls_back_to_artist_image(self, mock_discogs_service):
        """When search returns result with no artwork, fall back to artist image."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[make_discogs_result(release_id=28138, artwork_url=None)]
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
    async def test_falls_back_to_label_image(self, mock_discogs_service):
        """When artist image also unavailable, fall back to label image."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[make_discogs_result(release_id=28138, artwork_url=None)]
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
        assert results[0][1].artwork_url == "https://i.discogs.com/label-logo.jpg"
        mock_discogs_service.get_label_image.assert_called_once_with(233)

    @pytest.mark.asyncio
    async def test_no_fallback_when_artwork_exists(self, mock_discogs_service):
        """When search returns result with artwork, no fallback calls made."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=28138,
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
            results=[make_discogs_result(release_id=28138, artwork_url=None)]
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
            results=[make_discogs_result(release_id=28138, artwork_url=None)]
        )
        mock_discogs_service.get_release.return_value = None

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].artwork_url is None


# ---------------------------------------------------------------------------
# Tests: search_song_as_track (catalog-track-search §4.2, LML#301)
# ---------------------------------------------------------------------------


class TestSearchSongAsTrack:
    """SONG_AS_TRACK strategy: cross-reference song against Discogs, match
    releases back to library, emit matched_via TrackMatchHint per row."""

    @pytest.mark.asyncio
    async def test_no_song_returns_empty(self):
        db = AsyncMock()
        results, matched_via = await search_song_as_track(db, None)
        assert results == []
        assert matched_via == {}
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blank_song_returns_empty(self):
        db = AsyncMock()
        results, matched_via = await search_song_as_track(db, "")
        assert results == []
        assert matched_via == {}

    @pytest.mark.asyncio
    async def test_no_discogs_service_returns_empty(self):
        """Without a Discogs service, the strategy can't run and must no-op."""
        db = AsyncMock()
        results, matched_via = await search_song_as_track(
            db, "vi scose poise", discogs_service=None
        )
        assert results == []
        assert matched_via == {}
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_discogs_releases_returns_empty(self):
        db = AsyncMock()
        svc = AsyncMock()
        svc.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="vi scose poise", artist=None, releases=[], total=0
        )

        results, matched_via = await search_song_as_track(db, "vi scose poise", discogs_service=svc)

        assert results == []
        assert matched_via == {}
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_library_miss_returns_empty(self):
        """Discogs returns releases but none match in library — empty result."""
        db = AsyncMock()
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

        results, matched_via = await search_song_as_track(db, "unknown song", discogs_service=svc)

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

        results, matched_via = await search_song_as_track(db, "vi scose poise", discogs_service=svc)

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

        results, matched_via = await search_song_as_track(db, "No Way Back", discogs_service=svc)

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

        results, matched_via = await search_song_as_track(db, "vi scose poise", discogs_service=svc)

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

        results, matched_via = await search_song_as_track(db, "vi scose poise", discogs_service=svc)

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

        results, matched_via = await search_song_as_track(db, "No Way Back", discogs_service=svc)

        assert results == [va_album]
        assert matched_via[11111][0].artist_credit == "Adonis"
        queries = [call.kwargs["query"] for call in db.search.await_args_list]
        assert any(q.startswith("Various ") for q in queries)


# ---------------------------------------------------------------------------
# Tests: _log_track_validation (A7 / LML#344 audit instrumentation)
# ---------------------------------------------------------------------------


class TestLogTrackValidation:
    """Audit instrumentation for the two validate_track_on_release call sites.

    LML#344 wants to know how often the step-3b validation runs after
    TRACK_ON_COMPILATION's inline validation already vetted the same release,
    and how often the two verdicts diverge. The helper records each
    validation call with a source label so downstream Sentry analysis can
    split (release_id, verdict) pairs by source.

    Two surfaces:
      - Sentry breadcrumb every call (always-on; trace explorer queries against it).
      - Structured INFO log on a 1% sample (cheap Railway-log grep target).

    Sample rate is parameterizable so tests can force the deterministic branch.
    """

    def test_records_breadcrumb_on_every_call(self):
        """Every call must produce a Sentry breadcrumb (always-on for trace explorer)."""
        with patch("lookup.orchestrator.sentry_sdk.add_breadcrumb") as mock_breadcrumb:
            _log_track_validation(
                source="compilation_inline",
                release_id=12345,
                song="VI Scose Poise",
                artist="Autechre",
                verdict=True,
                latency_ms=42.5,
                sample_rate=0.0,  # disable log sampling; only breadcrumb path
            )
        mock_breadcrumb.assert_called_once()
        call = mock_breadcrumb.call_args
        assert call.kwargs["category"] == "track_validation"
        data = call.kwargs["data"]
        assert data["source"] == "compilation_inline"
        assert data["release_id"] == 12345
        assert data["song"] == "VI Scose Poise"
        assert data["artist"] == "Autechre"
        assert data["verdict"] is True
        assert data["latency_ms"] == 42.5

    def test_samples_info_log_at_configured_rate(self):
        """When the random sample fires, an INFO log is emitted alongside the breadcrumb."""
        with (
            patch("lookup.orchestrator.random.random", return_value=0.005),  # 0.5% < 1% → fires
            patch("lookup.orchestrator.logger") as mock_logger,
            patch("lookup.orchestrator.sentry_sdk.add_breadcrumb"),
        ):
            _log_track_validation(
                source="step_3b",
                release_id=1,
                song="x",
                artist="y",
                verdict=False,
                latency_ms=1.0,
                sample_rate=0.01,
            )
        # logger.info called with the structured payload
        info_calls = [c for c in mock_logger.info.call_args_list if "track_validation" in str(c)]
        assert len(info_calls) == 1

    def test_does_not_sample_info_log_when_rate_misses(self):
        """When the random draw exceeds the sample rate, no INFO log fires.

        Even at 1% sampling we still need the *deterministic* off-path so
        Railway logs aren't flooded by every /lookup. The breadcrumb path
        runs unconditionally; only the structured log is gated.
        """
        with (
            patch("lookup.orchestrator.random.random", return_value=0.99),  # 99% > 1% → no fire
            patch("lookup.orchestrator.logger") as mock_logger,
            patch("lookup.orchestrator.sentry_sdk.add_breadcrumb"),
        ):
            _log_track_validation(
                source="step_3b",
                release_id=1,
                song="x",
                artist="y",
                verdict=False,
                latency_ms=1.0,
                sample_rate=0.01,
            )
        info_calls = [c for c in mock_logger.info.call_args_list if "track_validation" in str(c)]
        assert len(info_calls) == 0

    def test_handles_sentry_sdk_failure(self):
        """A Sentry SDK exception must not break /lookup.

        Mirrors the swallow-and-log pattern in _log_album_title_fallback /
        _log_resolver_pre_pass / _log_search_budget_exceeded. The audit
        layer is non-essential — it cannot be allowed to take down the
        request path.
        """
        with patch(
            "lookup.orchestrator.sentry_sdk.add_breadcrumb",
            side_effect=RuntimeError("sentry exploded"),
        ):
            # Must not raise.
            _log_track_validation(
                source="compilation_inline",
                release_id=1,
                song="x",
                artist="y",
                verdict=True,
                latency_ms=1.0,
                sample_rate=0.0,
            )

    def test_handles_null_artist(self):
        """Some call paths pass artist=None; the helper must accept it."""
        with patch("lookup.orchestrator.sentry_sdk.add_breadcrumb") as mock_breadcrumb:
            _log_track_validation(
                source="step_3b",
                release_id=1,
                song="x",
                artist=None,
                verdict=False,
                latency_ms=1.0,
                sample_rate=0.0,
            )
        data = mock_breadcrumb.call_args.kwargs["data"]
        assert data["artist"] is None
