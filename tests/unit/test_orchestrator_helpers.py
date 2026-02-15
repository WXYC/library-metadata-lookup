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
from lookup.orchestrator import (
    build_context_message,
    fetch_artwork_for_items,
    filter_results_by_artist,
    filter_results_by_track_validation,
    resolve_albums_for_track,
    search_library_with_fallback,
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
    async def test_falls_back_to_artist_only(self, mock_library_db):
        item = make_library_item(
            id=2,
            artist="Queen",
            title="The Game",
            call_letters="Q",
            release_call_number=2,
        )
        mock_library_db.search.side_effect = [
            [],  # artist + album
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

        results, fallback = await search_library_with_fallback(
            mock_library_db, parsed, ["Unknown Album"]
        )
        assert len(results) == 1
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
        mock_discogs_service.get_label_image.return_value = (
            "https://i.discogs.com/label-logo.jpg"
        )

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
