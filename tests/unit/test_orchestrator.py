"""Unit tests for lookup/orchestrator.py - the core search pipeline.

These tests verify perform_lookup() orchestrates the full pipeline:
1. Artist spelling correction
2. Album resolution from Discogs
3. Search strategy pipeline execution
4. Fallback track validation
5. Artwork fetch
6. Context message generation

All external dependencies (LibraryDB, DiscogsService) are mocked.
"""

from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import DiscogsSearchResponse
from generated.api_models import (
    DiscogsReleaseInfo,
    DiscogsTrackReleasesResponse,
    TrackMatchSource,
)
from library.models import LibraryItem
from lookup.models import LookupRequest, LookupResponse
from lookup.orchestrator import perform_lookup
from tests.conftest import make_lml_telemetry
from tests.factories import make_discogs_result, make_library_item

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def telemetry():
    """Create a telemetry tracker for tests."""
    return make_lml_telemetry()


@pytest.fixture
def queen_item():
    return LibraryItem(
        id=1,
        artist="Queen",
        title="A Night at the Opera",
        call_letters="Q",
        artist_call_number=1,
        release_call_number=1,
        genre="Rock",
        format="CD",
    )


@pytest.fixture
def queen_game_item():
    return LibraryItem(
        id=2,
        artist="Queen",
        title="The Game",
        call_letters="Q",
        artist_call_number=1,
        release_call_number=2,
        genre="Rock",
        format="CD",
    )


@pytest.fixture
def stereolab_item():
    return LibraryItem(
        id=10,
        artist="Stereolab",
        title="Emperor Tomato Ketchup",
        call_letters="S",
        artist_call_number=1,
        release_call_number=1,
        genre="Rock",
        format="CD",
    )


@pytest.fixture
def compilation_item():
    return LibraryItem(
        id=20,
        artist="Various Artists - Rock - D",
        title="Disco Not Disco",
        call_letters="V",
        artist_call_number=1,
        release_call_number=1,
        genre="Rock",
        format="CD",
    )


@pytest.fixture
def joni_self_titled():
    return LibraryItem(
        id=19,
        artist="Joni Mitchell",
        title="Joni Mitchell",
        call_letters="MI",
        artist_call_number=8,
        release_call_number=1,
        genre="Rock",
        format="Vinyl",
    )


@pytest.fixture
def joni_court_and_spark():
    return LibraryItem(
        id=20,
        artist="Joni Mitchell",
        title="Court and Spark",
        call_letters="MI",
        artist_call_number=8,
        release_call_number=6,
        genre="Rock",
        format="Vinyl",
    )


# ---------------------------------------------------------------------------
# Tests: perform_lookup - basic cases
# ---------------------------------------------------------------------------


class TestPerformLookupBasic:
    """Test the full perform_lookup pipeline for basic cases."""

    @pytest.mark.asyncio
    async def test_artist_and_album_direct_match(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """Direct match: artist + album finds results immediately."""
        mock_library_db.search.return_value = [queen_item]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=12345,
                    album="A Night at the Opera",
                    artist="Queen",
                    artwork_url="https://example.com/cover.jpg",
                )
            ]
        )

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Play A Night at the Opera by Queen",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert isinstance(response, LookupResponse)
        assert len(response.results) == 1
        assert response.results[0].library_item.artist == "Queen"
        assert response.results[0].library_item.title == "A Night at the Opera"
        assert response.search_type == "direct"
        assert response.song_not_found is False

    @pytest.mark.asyncio
    async def test_no_results_returns_empty(self, mock_library_db, mock_discogs_service, telemetry):
        """When nothing matches, return empty results."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None

        request = LookupRequest(
            artist="Nonexistent Band",
            song="Unknown Song",
            raw_message="Play Unknown Song by Nonexistent Band",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert isinstance(response, LookupResponse)
        assert len(response.results) == 0

    @pytest.mark.asyncio
    async def test_no_discogs_service_still_works(self, mock_library_db, telemetry, queen_item):
        """Pipeline works without Discogs (artwork will be None)."""
        mock_library_db.search.return_value = [queen_item]

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Play A Night at the Opera by Queen",
        )

        response = await perform_lookup(request, mock_library_db, None, telemetry)

        assert len(response.results) == 1
        assert response.results[0].library_item.artist == "Queen"
        assert response.results[0].artwork is None

    @pytest.mark.asyncio
    async def test_song_as_track_emits_matched_via_on_result_item(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Song-only query 'vi scose poise' surfaces Confield with matched_via populated.

        Verifies the full wiring: SONG_AS_TRACK strategy runs after SONG_AS_ARTIST
        returns empty, the result row's matched_via dict is plumbed through
        SearchState into LookupResultItem.matched_via, and the TrackMatchHint
        records source=discogs_release with the track title.
        """
        confield = make_library_item(id=60359, artist="Autechre", title="Confield")

        # SONG_AS_ARTIST does: db.search(query=song) → []
        # SONG_AS_TRACK does: db.search(query=album) → [confield]
        async def search_side_effect(query, **kwargs):
            return [confield] if query and "confield" in query.lower() else []

        mock_library_db.search.side_effect = search_side_effect
        mock_discogs_service.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
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
        # SONG_AS_ARTIST queries Discogs by artist; return empty so it falls through.
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(song="vi scose poise", raw_message="vi scose poise")

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert len(response.results) == 1
        item = response.results[0]
        assert item.library_item.artist == "Autechre"
        assert item.library_item.title == "Confield"
        assert item.matched_via is not None
        assert len(item.matched_via) == 1
        hint = item.matched_via[0]
        assert hint.title == "vi scose poise"
        assert hint.source == TrackMatchSource.discogs_release

    @pytest.mark.asyncio
    async def test_matched_via_absent_on_non_track_match(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """When the result came via ARTIST_PLUS_ALBUM, matched_via is None.

        Guards against accidental population of matched_via on non-track-driven
        results — the field is reserved for track-search provenance per plan §5.1.
        """
        mock_library_db.search.return_value = [queen_item]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="A Night at the Opera by Queen",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert len(response.results) == 1
        assert response.results[0].matched_via is None


# ---------------------------------------------------------------------------
# Tests: perform_lookup - artist correction
# ---------------------------------------------------------------------------


class TestPerformLookupArtistCorrection:
    """Test that artist spelling is corrected before searching."""

    @pytest.mark.asyncio
    async def test_corrects_artist_spelling(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """Misspelled artist gets corrected via fuzzy match."""
        mock_library_db.find_similar_artist.return_value = "Living Colour"
        mock_library_db.search.return_value = [
            make_library_item(id=5, artist="Living Colour", title="Vivid", call_letters="L")
        ]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Living Color",
            raw_message="Play something by Living Color",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        # Artist should be corrected
        assert response.corrected_artist == "Living Colour"
        mock_library_db.find_similar_artist.assert_called_once_with("Living Color")

    @pytest.mark.asyncio
    async def test_artist_correction_and_album_resolution_both_execute(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Artist correction and album resolution should both run, with the
        corrected artist applied before search pipeline executes."""
        mock_library_db.find_similar_artist.return_value = "Stereolab"
        mock_library_db.search.return_value = [
            make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        ]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Stereolba",
            song="Percolator",
            raw_message="Stereolba - Percolator",
        )

        with (
            patch(
                "lookup.orchestrator.lookup_releases_by_track",
                new_callable=AsyncMock,
                return_value=[("Stereolab", "Emperor Tomato Ketchup")],
            ),
            patch(
                "lookup.orchestrator.fetch_artwork_for_items",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        mock_library_db.find_similar_artist.assert_called_once_with("Stereolba")
        assert response.corrected_artist == "Stereolab"
        assert telemetry.steps.get("album_lookup") is not None


# ---------------------------------------------------------------------------
# Tests: perform_lookup - album resolution from Discogs
# ---------------------------------------------------------------------------


class TestPerformLookupAlbumResolution:
    """Test album resolution when song is provided without album."""

    @pytest.mark.asyncio
    async def test_resolves_album_from_discogs_when_song_only(
        self, mock_library_db, mock_discogs_service, telemetry, stereolab_item
    ):
        """When song + artist given but no album, Discogs resolves album names."""
        mock_library_db.find_similar_artist.return_value = None
        mock_library_db.search.return_value = [stereolab_item]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Stereolab",
            song="Percolator",
            raw_message="Play Percolator by Stereolab",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Stereolab", "Emperor Tomato Ketchup")],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert len(response.results) >= 1
        assert response.song_not_found is False

    @pytest.mark.asyncio
    async def test_track_validation_filters_album_resolved_results(
        self,
        mock_library_db,
        mock_discogs_service,
        telemetry,
        joni_self_titled,
        joni_court_and_spark,
    ):
        """Album resolution may return false positives; track validation should filter them.

        Scenario: "Help Me by Joni Mitchell"
        - Discogs resolves albums: ["Court and Spark", "Joni Mitchell"]
        - Library matches both (self-titled + Court and Spark)
        - Track validation confirms "Help Me" is on Court and Spark but NOT self-titled
        - Only Court and Spark should remain in results
        """
        mock_library_db.find_similar_artist.return_value = None
        # search_library_with_fallback searches each album:
        # 1. "Joni Mitchell Court and Spark" -> Court and Spark
        # 2. "Joni Mitchell Joni Mitchell" -> self-titled
        mock_library_db.search.side_effect = [
            [joni_court_and_spark],
            [joni_self_titled],
        ]

        # Discogs search for artwork (called per result in filter_results_by_track_validation)
        court_and_spark_discogs = make_discogs_result(
            release_id=1001,
            album="Court and Spark",
            artist="Joni Mitchell",
            artwork_url="https://example.com/court-and-spark.jpg",
        )
        self_titled_discogs = make_discogs_result(
            release_id=1002,
            album="Joni Mitchell",
            artist="Joni Mitchell",
            artwork_url="https://example.com/joni-mitchell.jpg",
        )
        # search() is called by filter_results_by_track_validation for each item,
        # then again by fetch_artwork_for_items
        mock_discogs_service.search.side_effect = [
            # track validation: search for Court and Spark
            DiscogsSearchResponse(results=[court_and_spark_discogs]),
            # track validation: search for Joni Mitchell (self-titled)
            DiscogsSearchResponse(results=[self_titled_discogs]),
            # artwork fetch for the one remaining result
            DiscogsSearchResponse(results=[court_and_spark_discogs]),
        ]

        # validate_track_on_release: "Help Me" IS on Court and Spark, NOT on self-titled
        mock_discogs_service.validate_track_on_release.side_effect = [True, False]

        request = LookupRequest(
            artist="Joni Mitchell",
            song="Help Me",
            raw_message="Play Help Me by Joni Mitchell",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Joni Mitchell", "Court and Spark"), ("Joni Mitchell", "Joni Mitchell")],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert len(response.results) == 1
        assert response.results[0].library_item.title == "Court and Spark"
        assert response.song_not_found is False
        assert response.search_type == "direct"


# ---------------------------------------------------------------------------
# Tests: perform_lookup - fallback and context messages
# ---------------------------------------------------------------------------


class TestPerformLookupFallback:
    """Test fallback behavior when exact match isn't found."""

    @pytest.mark.asyncio
    async def test_song_not_found_sets_context_message(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item, queen_game_item
    ):
        """When song isn't found, fall back to artist albums with context message."""
        # First search (artist+album) returns empty, fallback to artist-only
        mock_library_db.find_similar_artist.return_value = None
        mock_library_db.search.side_effect = [
            [],  # artist + song
            [queen_item, queen_game_item],  # artist only
        ]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.validate_track_on_release.return_value = False

        request = LookupRequest(
            artist="Queen",
            song="Unknown Track",
            raw_message="Play Unknown Track by Queen",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.song_not_found is True
        assert response.context_message is not None
        assert "Queen" in response.context_message

    @pytest.mark.asyncio
    async def test_track_validation_filters_fallback_results(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item, queen_game_item
    ):
        """When fallback returns all artist albums, track validation filters to correct one."""
        mock_library_db.find_similar_artist.return_value = None
        # Fallback: artist-only returns both albums
        mock_library_db.search.side_effect = [
            [],  # artist + song
            [queen_item, queen_game_item],  # artist only
        ]

        # Discogs validates: "Bohemian Rhapsody" is on "A Night at the Opera" but not "The Game"
        search_result = make_discogs_result(
            release_id=12345,
            album="A Night at the Opera",
            artist="Queen",
            artwork_url="https://example.com/opera.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[search_result])
        # validate_track_on_release: True for queen_item, False for queen_game_item
        mock_discogs_service.validate_track_on_release.side_effect = [True, False]

        request = LookupRequest(
            artist="Queen",
            song="Bohemian Rhapsody",
            raw_message="Play Bohemian Rhapsody by Queen",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        # Track validation should narrow it down
        assert response.song_not_found is False
        assert len(response.results) == 1
        assert response.results[0].library_item.title == "A Night at the Opera"

    @pytest.mark.asyncio
    async def test_promotes_album_known_to_contain_track_from_local_cache(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """When per-result Discogs validation can't confirm any artist-fallback
        candidate, the orchestrator should consult the local Discogs PG cache
        for "releases by this artist containing this track" and promote the
        matching library album.

        Reproduces "bucky skank by lee scratch perry": the artist-only fallback
        returned 5 unrelated Lee Perry albums, all failed validation, and the
        library album that actually contains the song ("Live at Maritime Hall")
        was never surfaced — even though the local cache had the answer.
        """
        # Five fallback albums that don't contain the track
        fallback_items = [
            LibraryItem(
                id=12663,
                artist="Lee 'Scratch' Perry",
                title="Chicken Scratch",
                call_letters="Pe",
                artist_call_number=1,
                release_call_number=1,
                genre="Reggae",
                format="vinyl",
            ),
            LibraryItem(
                id=12664,
                artist="Lee 'Scratch' Perry",
                title='Ooh! Wah! 12"',
                call_letters="Pe",
                artist_call_number=1,
                release_call_number=2,
                genre="Reggae",
                format="vinyl",
            ),
        ]
        # The actual answer (in the library, but not in the fallback's top-N FTS slice)
        maritime = LibraryItem(
            id=12682,
            artist="Lee 'Scratch' Perry",
            title="Live at Maritime Hall",
            call_letters="Pe",
            artist_call_number=1,
            release_call_number=20,
            genre="Reggae",
            format="cd",
        )

        mock_library_db.find_similar_artist.return_value = None

        # Mirror the production FTS5 behavior: queries containing the song words
        # ("bucky"/"skank") match nothing because no album *title* contains
        # those tokens. Only the artist-only and album-title-based queries hit.
        async def fake_search(query=None, **kwargs):
            q = (query or "").lower()
            if "bucky" in q or "skank" in q:
                return []
            if "maritime" in q:
                return [maritime]
            if "scratch" in q or "perry" in q:
                return fallback_items
            return []

        mock_library_db.search.side_effect = fake_search

        # Per-result validation fails for all fallback items (none contain the track)
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.validate_track_on_release.return_value = False

        from discogs.models import ReleaseInfo, TrackReleasesResponse

        # Compilation strategy's two API calls return nothing (mirrors prod where
        # the artist-only fallback isn't replaced by a compilation hit)
        mock_discogs_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Bucky Skank",
                artist="Lee 'Scratch' Perry",
                releases=[],
                total=0,
                cached=False,
            )
        )

        # The local PG cache holds the answer — Maritime Hall has Bucky Skank
        mock_discogs_service.cache_service = AsyncMock()
        mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
            return_value=[
                ReleaseInfo(
                    album="Lee Scratch Perry Live At Maritime Hall",
                    artist="Lee 'Scratch' Perry",
                    release_id=2865555,
                    release_url="https://discogs.com/release/2865555",
                    is_compilation=False,
                ),
            ]
        )

        request = LookupRequest(
            artist="Lee 'Scratch' Perry",
            song="Bucky Skank",
            raw_message="bucky skank by lee scratch perry",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        titles = [r.library_item.title for r in response.results]
        assert "Live at Maritime Hall" in titles, (
            f"Cache-known album should be promoted; got: {titles}"
        )
        assert response.song_not_found is False, (
            "Promoting a confirmed track-bearing album should clear song_not_found"
        )

    @pytest.mark.asyncio
    async def test_song_matching_album_title_clears_song_not_found(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """When the requested "song" is the title of an album by the artist,
        treat it as a direct album match — not a song-not-found fallback.

        Reproduces "on patrol, sun araw": request-o-matic routed it as
        ``song="On Patrol"`` / ``artist="Sun Araw"``. The Sun Araw album
        "On Patrol" is in the library and surfaced by the artist+song FTS
        branch, but Discogs has no track called "On Patrol" on any Sun Araw
        release, so per-result validation + cached-track promotion both
        fail and ``song_not_found`` stays True. The bot then says
        '"On Patrol" is not on any album in the library' — about the album
        sitting in its own result list.
        """
        on_patrol = LibraryItem(
            id=42,
            artist="Sun Araw",
            title="On Patrol",
            call_letters="SU",
            artist_call_number=138,
            release_call_number=1,
            genre="Rock",
            format="cd",
        )

        mock_library_db.find_similar_artist.return_value = None

        # search_library_with_fallback call order for (artist + song, no album):
        #   1. artist + song FTS -> finds "On Patrol" (title matches the song term)
        async def fake_search(query=None, **kwargs):
            q = (query or "").lower()
            if "sun araw" in q:
                return [on_patrol]
            return []

        mock_library_db.search.side_effect = fake_search

        # Per-result validation fails: no track named "On Patrol" on this release.
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.validate_track_on_release.return_value = False

        # Cache-known-track promotion also turns up empty (no Sun Araw release
        # in the cache contains a track titled "On Patrol").
        mock_discogs_service.cache_service = AsyncMock()
        mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(return_value=[])

        request = LookupRequest(
            artist="Sun Araw",
            song="On Patrol",
            raw_message="on patrol, sun araw",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert len(response.results) == 1
        assert response.results[0].library_item.title == "On Patrol"
        assert response.song_not_found is False, (
            "The album titled 'On Patrol' is in results — the user wanted "
            "that album, not a track. song_not_found should be cleared."
        )
        assert response.context_message is None, (
            "Should not emit the misleading 'not on any album' message when "
            "results contain an album titled the same as the requested song."
        )


# ---------------------------------------------------------------------------
# Tests: perform_lookup - compilation search
# ---------------------------------------------------------------------------


class TestPerformLookupCompilations:
    """Test compilation search when direct search fails."""

    @pytest.mark.asyncio
    async def test_finds_song_on_compilation(
        self, mock_library_db, mock_discogs_service, telemetry, compilation_item
    ):
        """When song not on any artist album, find it on a compilation."""
        mock_library_db.find_similar_artist.return_value = None

        # A fallback item that would be returned by artist-only search
        fallback_item = make_library_item(
            id=99,
            artist="Some Artist",
            title="Some Album",
            call_letters="S",
        )

        # search_library_with_fallback call order:
        # 1. artist + song -> empty (no album match)
        # 2. artist only -> returns fallback (triggers song_not_found=True)
        # Then search_compilations_for_track is triggered:
        # 3. keyword search -> returns compilation_item
        # Then search_album_fuzzy is called for the Discogs album title:
        # 4. exact search for "Disco Not Disco" -> returns compilation_item
        mock_library_db.search.side_effect = [
            [],  # search_library_with_fallback: artist + song
            [fallback_item],  # search_library_with_fallback: artist only (song_not_found=True)
            [compilation_item],  # search_compilations_for_track: keyword search
            [compilation_item],  # search_album_fuzzy: exact search for Discogs album
        ]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Some Artist",
            song="Disco Song",
            raw_message="Play Disco Song by Some Artist",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Various Artists", "Disco Not Disco")],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.found_on_compilation is True
        assert len(response.results) >= 1
        assert response.context_message is not None
        assert "Found" in response.context_message

    @pytest.mark.asyncio
    async def test_finds_song_on_compilation_when_artist_not_in_library(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Compilation search runs when the track artist has zero library entries.

        Scenario: "No Way Back by Adonis"
        - resolve_albums_for_track finds only VA releases -> song_not_found=True
        - search_library_with_fallback finds nothing for "Adonis" -> ([], False)
        - song_not_found from album resolution must propagate so
          TRACK_ON_COMPILATION still triggers
        """
        compilation_item = make_library_item(
            id=46602,
            artist="Various Artists - Electronic - T",
            title="Trax Records 20th Anniversary Collection",
            call_letters="V",
        )

        mock_library_db.find_similar_artist.return_value = None

        # search_library_with_fallback: artist+song -> empty, artist-only -> empty
        # search_compilations_for_track: keyword search -> compilation,
        #   search_album_fuzzy -> compilation
        mock_library_db.search.side_effect = [
            [],  # search_library_with_fallback: artist + song
            [],  # search_library_with_fallback: artist only (Adonis not in library)
            [compilation_item],  # search_compilations_for_track: keyword search
            [compilation_item],  # search_album_fuzzy: exact search for Discogs album
        ]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Adonis",
            song="No Way Back",
            raw_message="No Way Back by Adonis",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[
                ("Various Artists", "Trax Records 20th Anniversary Collection"),
            ],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.found_on_compilation is True
        assert len(response.results) >= 1
        assert response.results[0].library_item.title == "Trax Records 20th Anniversary Collection"
        assert response.context_message is not None
        assert "Found" in response.context_message

    @pytest.mark.asyncio
    async def test_finds_song_on_compilation_when_discogs_resolves_single(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Compilation search runs when Discogs resolves an artist single not in library.

        Scenario: "No Way Back by Adonis"
        - resolve_albums_for_track finds Adonis single -> (["No Way Back"], False)
        - search_library_with_fallback finds nothing for "Adonis" at all -> ([], True)
        - ARTIST_PLUS_ALBUM sets song_not_found=True from the fallback flag
        - TRACK_ON_COMPILATION triggers and finds the VA compilation
        """
        compilation_item = make_library_item(
            id=46602,
            artist="Various Artists - Electronic - T",
            title="Trax Records 20th Anniversary Collection",
            call_letters="V",
        )

        mock_library_db.find_similar_artist.return_value = None

        # search_library_with_fallback call order:
        # 1. artist + album "Adonis No Way Back" -> empty
        # 2. artist + song "Adonis No Way Back" -> empty
        # 3. artist only "Adonis" -> empty
        # Then search_compilations_for_track:
        # 4. keyword search "adonis back" -> compilation
        # 5. search_album_fuzzy("No Way Back") exact -> rejected by album_title_acceptable
        # 6. search_album_fuzzy("No Way Back") fuzzy "back" -> rejected by similarity
        # 7. search_album_fuzzy("Trax Records...") exact -> compilation (VA match)
        mock_library_db.search.side_effect = [
            [],  # search_library_with_fallback: artist + album
            [],  # search_library_with_fallback: artist + song
            [],  # search_library_with_fallback: artist only
            [compilation_item],  # search_compilations_for_track: keyword search
            [compilation_item],  # search_album_fuzzy: exact "No Way Back" (filtered)
            [compilation_item],  # search_album_fuzzy: fuzzy "back" (rejected)
            [compilation_item],  # search_album_fuzzy: exact "Trax Records..." (match)
        ]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Adonis",
            song="No Way Back",
            raw_message="No Way Back by Adonis",
        )

        # Discogs API with artist field filter returns only the single;
        # with artist as keyword, also returns VA compilations
        async def mock_track_lookup(track, artist=None, artist_as_keyword=False, **kwargs):
            if artist and not artist_as_keyword:
                return [("Adonis", "No Way Back")]
            if artist and artist_as_keyword:
                return [
                    ("Adonis", "No Way Back"),
                    ("Various Artists", "Trax Records 20th Anniversary Collection"),
                ]
            return []

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            side_effect=mock_track_lookup,
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.found_on_compilation is True
        assert len(response.results) >= 1
        assert response.results[0].library_item.title == "Trax Records 20th Anniversary Collection"
        assert response.context_message is not None
        assert "Found" in response.context_message

    @pytest.mark.asyncio
    async def test_compilation_search_uses_artist_as_keyword(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Compilation search uses artist as keyword (q param) to find VA compilations.

        The Discogs API `artist` param filters by release-level artist, which
        excludes VA compilations. Using `artist_as_keyword=True` puts the artist
        in the `q` param instead, matching track-level credits on compilations.
        """
        from discogs.models import ReleaseInfo, TrackReleasesResponse

        compilation_item = make_library_item(
            id=46602,
            artist="Various Artists - Hiphop",
            title="Trax Records 20th Anniversary Collection",
            call_letters="V",
        )

        mock_library_db.find_similar_artist.return_value = None

        # db.search call order:
        # 1. search_library_with_fallback: "Adonis No Way Back" (artist+album) -> []
        # 2. search_library_with_fallback: "Adonis No Way Back" (artist+song) -> []
        # 3. search_library_with_fallback: "Adonis" (artist only) -> []
        # 4. search_compilations_for_track: keyword "adonis back" -> []
        # 5. search_album_fuzzy("No Way Back"): exact -> []
        # 6. search_album_fuzzy("Trax Records..."): exact -> [compilation_item]
        mock_library_db.search.side_effect = [
            [],  # 1: artist + album
            [],  # 2: artist + song
            [],  # 3: artist only
            [],  # 4: keyword search
            [],  # 5: search_album_fuzzy("No Way Back") exact
            [compilation_item],  # 6: search_album_fuzzy("Trax Records...") exact
        ]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        # search_compilations_for_track calls discogs_service.search_releases_by_track
        # directly (library-first approach), not lookup_releases_by_track.
        adonis_release = ReleaseInfo(
            album="No Way Back",
            artist="Adonis",
            release_id=1001,
            release_url="https://www.discogs.com/release/1001",
            is_compilation=False,
        )
        trax_release = ReleaseInfo(
            album="Trax Records 20th Anniversary Collection",
            artist="Various Artists",
            release_id=1002,
            release_url="https://www.discogs.com/release/1002",
            is_compilation=True,
        )

        async def mock_discogs_search(track, artist=None, artist_as_keyword=False, **kwargs):
            if not artist_as_keyword:
                return TrackReleasesResponse(
                    track=track, artist=artist, releases=[adonis_release], total=1
                )
            return TrackReleasesResponse(
                track=track,
                artist=artist,
                releases=[adonis_release, trax_release],
                total=2,
            )

        mock_discogs_service.search_releases_by_track = AsyncMock(side_effect=mock_discogs_search)
        mock_discogs_service.validate_track_on_release = AsyncMock(return_value=True)

        request = LookupRequest(
            artist="Adonis",
            song="No Way Back",
            raw_message="No Way Back by Adonis",
        )

        # resolve_albums_for_track still uses lookup_releases_by_track
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Adonis", "No Way Back")],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.found_on_compilation is True
        assert len(response.results) >= 1
        assert response.results[0].library_item.title == "Trax Records 20th Anniversary Collection"

    @pytest.mark.asyncio
    async def test_compilation_results_skip_track_validation(
        self, mock_library_db, mock_discogs_service, telemetry, compilation_item
    ):
        """When results come from compilation search, step 3b (filter_results_by_track_validation)
        should be skipped because the track was already validated in search_compilations_for_track.

        Re-running validation would make unnecessary Discogs API calls.
        """
        mock_library_db.find_similar_artist.return_value = None

        fallback_item = make_library_item(
            id=99,
            artist="Some Artist",
            title="Some Album",
            call_letters="S",
        )

        mock_library_db.search.side_effect = [
            [],  # search_library_with_fallback: artist + song
            [fallback_item],  # search_library_with_fallback: artist only
            [compilation_item],  # search_compilations_for_track: keyword search
            [compilation_item],  # search_album_fuzzy: exact search
        ]

        # If filter_results_by_track_validation runs, it calls discogs_service.search
        # for each result. We track whether this happens.
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Some Artist",
            song="Disco Song",
            raw_message="Play Disco Song by Some Artist",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Various Artists", "Disco Not Disco")],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.found_on_compilation is True
        # Compilation results should NOT be re-validated (already validated in
        # search_compilations_for_track). search calls come from:
        # 1. Artist fallback validation: 1 call (for fallback_item → no Discogs match)
        # 2. Artwork fetch: 1 call (for compilation_item)
        # Without the artist fallback validation this would be 1 call.
        # If compilation results were ALSO re-validated, count would be 3+.
        assert mock_discogs_service.search.call_count <= 2


# ---------------------------------------------------------------------------
# Tests: perform_lookup - artwork
# ---------------------------------------------------------------------------


class TestPerformLookupArtwork:
    """Test artwork fetching for results."""

    @pytest.mark.asyncio
    async def test_fetches_artwork_for_results(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """Results include artwork from Discogs."""
        mock_library_db.search.return_value = [queen_item]
        mock_library_db.find_similar_artist.return_value = None

        artwork = make_discogs_result(
            release_id=12345,
            album="A Night at the Opera",
            artist="Queen",
            artwork_url="https://example.com/cover.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[artwork])

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Play A Night at the Opera by Queen",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert len(response.results) == 1
        assert response.results[0].artwork is not None
        assert response.results[0].artwork.artwork_url == "https://example.com/cover.jpg"


# ---------------------------------------------------------------------------
# Tests: perform_lookup - reconciled identity
# ---------------------------------------------------------------------------


class TestPerformLookupReconciledIdentity:
    """Test that perform_lookup populates reconciled_identity for results."""

    @pytest.mark.asyncio
    async def test_populates_reconciled_identity_when_entity_store_has_artist(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """Each result gets ReconciledIdentity from EntityStore.get_identity."""
        from entity.store import Identity

        mock_library_db.search.return_value = [queen_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        entity_store = AsyncMock()
        entity_store.get_identity = AsyncMock(
            return_value=Identity(
                id=42,
                library_name="Queen",
                discogs_artist_id=7894,
                wikidata_qid="Q15862",
                spotify_artist_id="1dfeR4HaWDbWqFHLkxsg1d",
            )
        )

        request = LookupRequest(artist="Queen", album="A Night at the Opera", raw_message="...")

        response = await perform_lookup(
            request, mock_library_db, mock_discogs_service, telemetry, entity_store=entity_store
        )

        assert len(response.results) == 1
        identity = response.results[0].reconciled_identity
        assert identity is not None
        assert identity.discogs_artist_id == 7894
        assert identity.wikidata_qid == "Q15862"
        assert identity.spotify_artist_id == "1dfeR4HaWDbWqFHLkxsg1d"
        # Unset fields are None on the schema
        assert identity.musicbrainz_artist_id is None

    @pytest.mark.asyncio
    async def test_omits_reconciled_identity_when_entity_store_is_none(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """When EntityStore isn't configured, reconciled_identity is left None."""
        mock_library_db.search.return_value = [queen_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(artist="Queen", album="A Night at the Opera", raw_message="...")

        # No entity_store kwarg — defaults to None
        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert len(response.results) == 1
        assert response.results[0].reconciled_identity is None

    @pytest.mark.asyncio
    async def test_omits_reconciled_identity_when_artist_unknown_to_entity_store(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """Artists that don't appear in entity.identity get None — not an error."""
        mock_library_db.search.return_value = [queen_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        entity_store = AsyncMock()
        entity_store.get_identity = AsyncMock(return_value=None)

        request = LookupRequest(artist="Queen", album="A Night at the Opera", raw_message="...")

        response = await perform_lookup(
            request, mock_library_db, mock_discogs_service, telemetry, entity_store=entity_store
        )

        assert len(response.results) == 1
        assert response.results[0].reconciled_identity is None

    @pytest.mark.asyncio
    async def test_dedupes_lookup_per_artist_across_results(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item, queen_game_item
    ):
        """Two results for the same artist trigger only one EntityStore.get_identity call."""
        from entity.store import Identity

        mock_library_db.search.return_value = [queen_item, queen_game_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        entity_store = AsyncMock()
        entity_store.get_identity = AsyncMock(
            return_value=Identity(id=1, library_name="Queen", discogs_artist_id=7894)
        )

        request = LookupRequest(artist="Queen", raw_message="Queen")

        response = await perform_lookup(
            request, mock_library_db, mock_discogs_service, telemetry, entity_store=entity_store
        )

        assert len(response.results) == 2
        # Both results share the same reconciled identity
        assert response.results[0].reconciled_identity.discogs_artist_id == 7894
        assert response.results[1].reconciled_identity.discogs_artist_id == 7894
        # Only one DB lookup despite two results
        assert entity_store.get_identity.call_count == 1

    @pytest.mark.asyncio
    async def test_entity_store_exception_does_not_fail_lookup(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """A transient entity-store failure leaves reconciled_identity None
        rather than turning the whole /lookup response into a 500."""
        mock_library_db.search.return_value = [queen_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        entity_store = AsyncMock()
        entity_store.get_identity = AsyncMock(side_effect=ConnectionError("entity DB down"))

        request = LookupRequest(artist="Queen", album="A Night at the Opera", raw_message="...")

        response = await perform_lookup(
            request, mock_library_db, mock_discogs_service, telemetry, entity_store=entity_store
        )

        # Lookup still succeeds; the field is just absent for the failed artist.
        assert len(response.results) == 1
        assert response.results[0].reconciled_identity is None

    @pytest.mark.asyncio
    async def test_compilation_entries_skip_identity_lookup(
        self, mock_library_db, mock_discogs_service, telemetry, compilation_item
    ):
        """Compilation entries (artist='Various Artists - ...') aren't keyed in
        entity.identity, so they get reconciled_identity=None — not an error."""
        mock_library_db.search.return_value = [compilation_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        entity_store = AsyncMock()
        entity_store.get_identity = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="Various Artists - Rock - D",
            album="Disco Not Disco",
            raw_message="...",
        )

        response = await perform_lookup(
            request, mock_library_db, mock_discogs_service, telemetry, entity_store=entity_store
        )

        assert len(response.results) == 1
        assert response.results[0].reconciled_identity is None


# ---------------------------------------------------------------------------
# Tests: perform_lookup - ambiguous format
# ---------------------------------------------------------------------------


class TestPerformLookupAmbiguousFormat:
    """Test handling of ambiguous 'X - Y' format messages."""

    @pytest.mark.asyncio
    async def test_tries_both_interpretations(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """For 'Artist - Title' format, tries both orderings."""
        amps_item = make_library_item(
            id=61692,
            artist="Amps for Christ",
            title="Circuits",
        )

        mock_library_db.find_similar_artist.return_value = None
        # Alternative search: first interpretation finds results
        # search_with_alternative_interpretation does 2 db.search calls:
        # 1. query="Amps for Christ Edward" -> filtered by "Amps for Christ" (part1)
        # 2. query="Edward Amps for Christ" -> filtered by "Edward" (part2)
        mock_library_db.search.side_effect = [
            [amps_item],  # interpretation 1: "Amps for Christ" as artist -> matches
            [],  # interpretation 2: "Edward" as artist -> no matches
        ]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist=None,
            song=None,
            raw_message="Amps for Christ - Edward",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert len(response.results) >= 1
        assert response.search_type == "alternative"


# ---------------------------------------------------------------------------
# Tests: perform_lookup - track on artist album + compilation
# ---------------------------------------------------------------------------


class TestArtistAlbumPlusCompilation:
    """Test that tracks found on both an artist album and a compilation return both results.

    Bug: "Poison Dart" by "The Bug" is on London Zoo (artist album, library ID 54324)
    AND The Sound of Dub (VA compilation, library ID 47808). The pipeline should
    return both, but currently only returns the compilation because TRACK_ON_COMPILATION
    replaces artist fallback results and track validation is skipped.
    """

    @pytest.mark.asyncio
    async def test_artist_album_included_when_compilation_also_found(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Both London Zoo (artist album) and The Sound of Dub (compilation) should be returned.

        Scenario:
        1. resolve_albums_for_track returns nothing (Discogs track lookup fails/empty)
        2. ARTIST_PLUS_ALBUM: artist+song empty, falls back to artist-only → Bug albums
        3. TRACK_ON_COMPILATION: finds The Sound of Dub (compilation)
        4. Track validation should confirm London Zoo contains "Poison Dart"
        5. Both London Zoo and The Sound of Dub should appear in results
        """
        from discogs.models import ReleaseInfo, TrackReleasesResponse

        london_zoo = make_library_item(
            id=54324,
            artist="The Bug",
            title="London Zoo",
            call_letters="B",
            genre="Electronic",
        )
        pressure = make_library_item(
            id=54325,
            artist="The Bug",
            title="Pressure",
            call_letters="B",
            genre="Electronic",
            release_call_number=2,
        )
        sound_of_dub = make_library_item(
            id=47808,
            artist="various",
            title="The Sound of Dub",
            call_letters="V",
            genre="Reggae",
        )

        mock_library_db.find_similar_artist.return_value = None

        # db.search call order:
        # 1. search_library_with_fallback: artist+song "The Bug Poison Dart" → []
        # 2. search_library_with_fallback: artist only "The Bug" → [london_zoo, pressure]
        # 3. search_compilations_for_track: keyword "poison dart" → []
        # 4. search_album_fuzzy: "The Sound of Dub" → [sound_of_dub]
        mock_library_db.search.side_effect = [
            [],  # artist + song
            [london_zoo, pressure],  # artist only fallback
            [],  # keyword search (no library match for "poison dart")
            [sound_of_dub],  # search_album_fuzzy for Discogs album title
        ]

        # Discogs: search_releases_by_track finds The Sound of Dub (compilation)
        mock_discogs_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Poison Dart",
                artist="The Bug",
                releases=[
                    ReleaseInfo(
                        album="The Sound of Dub",
                        artist="Various Artists",
                        release_id=2308471,
                        release_url="https://www.discogs.com/release/2308471",
                        is_compilation=True,
                    ),
                ],
                total=1,
                cached=False,
            )
        )

        # Track validation: "Poison Dart" IS on London Zoo, NOT on Pressure
        london_zoo_discogs = make_discogs_result(
            release_id=1395903, album="London Zoo", artist="The Bug"
        )
        pressure_discogs = make_discogs_result(release_id=9999, album="Pressure", artist="The Bug")

        async def mock_discogs_search(request):
            album = request.album if hasattr(request, "album") else ""
            if album and "london" in album.lower():
                return DiscogsSearchResponse(results=[london_zoo_discogs])
            if album and "pressure" in album.lower():
                return DiscogsSearchResponse(results=[pressure_discogs])
            return DiscogsSearchResponse(results=[])

        mock_discogs_service.search = AsyncMock(side_effect=mock_discogs_search)

        async def mock_validate(release_id, track, artist):
            # Poison Dart is on London Zoo (1395903) and The Sound of Dub (2308471)
            return release_id in (1395903, 2308471)

        mock_discogs_service.validate_track_on_release = AsyncMock(side_effect=mock_validate)
        mock_discogs_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="The Bug",
            song="Poison Dart",
            raw_message="poison dart, the bug",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],  # Discogs track lookup returns nothing
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        titles = [r.library_item.title for r in response.results]
        assert "London Zoo" in titles, (
            f"Artist album 'London Zoo' should be in results, got: {titles}"
        )
        assert "The Sound of Dub" in titles, (
            f"Compilation 'The Sound of Dub' should be in results, got: {titles}"
        )
        assert response.found_on_compilation is True
        # Artist's own album should come first
        assert titles.index("London Zoo") < titles.index("The Sound of Dub")


# ---------------------------------------------------------------------------
# Tests: perform_lookup - external cache fallback (include_external_caches)
# ---------------------------------------------------------------------------


class TestPerformLookupExternalCacheFallback:
    """include_external_caches=True falls back to discogs/MB when library is empty.

    Used by the lossy-mojibake matcher in tubafrenzy to recover canonical artist
    names that aren't in the WXYC library catalog (~99% of the lossy bucket).
    """

    @pytest.mark.asyncio
    async def test_default_off_does_not_query_external(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Existing callers (no flag) see no behavior change and no external query."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(artist="Whoever", raw_message="Whoever")

        response = await perform_lookup(
            request,
            mock_library_db,
            mock_discogs_service,
            telemetry,
            discogs_cache=discogs_cache,
            mb_pg=mb_pg,
        )

        assert response.results == []
        assert response.external_source is None
        discogs_cache.search_artists_by_name.assert_not_called()
        mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_library_hit_marks_source_library_and_skips_external(
        self,
        mock_library_db,
        mock_discogs_service,
        telemetry,
        stereolab_item,
    ):
        """When the library returns results, external_source='library' and external caches aren't queried."""
        mock_library_db.search.return_value = [stereolab_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            artist="Stereolab",
            album="Emperor Tomato Ketchup",
            raw_message="Stereolab",
            include_external_caches=True,
        )

        response = await perform_lookup(
            request,
            mock_library_db,
            mock_discogs_service,
            telemetry,
            discogs_cache=discogs_cache,
            mb_pg=mb_pg,
        )

        assert len(response.results) == 1
        assert response.external_source == "library"
        discogs_cache.search_artists_by_name.assert_not_called()
        mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_discogs_when_library_empty(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """No library hit + flag set + discogs cache hit -> synthetic LookupResultItem."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(
            return_value=[
                {"id": 99, "name": "Astrid Øster Mortensen", "score": 0.71},
            ]
        )
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            artist="Astrid ster Mortenson",
            raw_message="Astrid ster Mortenson",
            include_external_caches=True,
        )

        response = await perform_lookup(
            request,
            mock_library_db,
            mock_discogs_service,
            telemetry,
            discogs_cache=discogs_cache,
            mb_pg=mb_pg,
        )

        assert response.external_source == "discogs"
        assert len(response.results) == 1
        assert response.results[0].library_item.artist == "Astrid Øster Mortensen"
        # No artwork enrichment for external candidates
        assert response.results[0].artwork is None
        # MB never queried because discogs hit
        mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_musicbrainz_when_discogs_empty(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(
            return_value=[
                {"id": "mb-uuid", "name": "Csillagrablók", "score": 0.83},
            ]
        )

        request = LookupRequest(
            artist="Csillagrablok",
            raw_message="Csillagrablok",
            include_external_caches=True,
        )

        response = await perform_lookup(
            request,
            mock_library_db,
            mock_discogs_service,
            telemetry,
            discogs_cache=discogs_cache,
            mb_pg=mb_pg,
        )

        assert response.external_source == "musicbrainz"
        assert len(response.results) == 1
        assert response.results[0].library_item.artist == "Csillagrablók"

    @pytest.mark.asyncio
    async def test_external_source_none_when_nothing_matches(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            artist="ZZZ Nothing",
            raw_message="ZZZ Nothing",
            include_external_caches=True,
        )

        response = await perform_lookup(
            request,
            mock_library_db,
            mock_discogs_service,
            telemetry,
            discogs_cache=discogs_cache,
            mb_pg=mb_pg,
        )

        assert response.results == []
        assert response.external_source is None

    @pytest.mark.asyncio
    async def test_no_external_query_when_no_typed_field(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Phase 1.7: a bare raw_message with no typed field (LABEL_NAME case) skips fallback."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        discogs_cache.search_releases_by_title = AsyncMock(return_value=[])
        discogs_cache.search_tracks_by_title = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            raw_message="ESP Disk'",
            include_external_caches=True,
        )

        response = await perform_lookup(
            request,
            mock_library_db,
            mock_discogs_service,
            telemetry,
            discogs_cache=discogs_cache,
            mb_pg=mb_pg,
        )

        assert response.external_source is None
        discogs_cache.search_artists_by_name.assert_not_called()
        discogs_cache.search_releases_by_title.assert_not_called()
        discogs_cache.search_tracks_by_title.assert_not_called()
        mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_album_skeleton_falls_back_to_discogs_releases(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Phase 1.7: RELEASE_TITLE skeleton -> discogs release fuzzy hit -> synthetic result."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        discogs_cache.search_releases_by_title = AsyncMock(
            return_value=[
                {"id": 12345, "title": "DOGA", "artist": "Juana Molina", "score": 0.81},
            ]
        )
        discogs_cache.search_tracks_by_title = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            album="DOG",
            raw_message="DOG",
            include_external_caches=True,
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache=discogs_cache,
                mb_pg=mb_pg,
            )

        assert response.external_source == "discogs"
        assert len(response.results) == 1
        item = response.results[0].library_item
        assert item.artist == "Juana Molina"
        assert item.title == "DOGA"
        # Artist branch must NOT have fired; only release branch.
        discogs_cache.search_artists_by_name.assert_not_called()
        discogs_cache.search_tracks_by_title.assert_not_called()
        discogs_cache.search_releases_by_title.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_song_skeleton_falls_back_to_discogs_tracks(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Phase 1.7: SONG_TITLE skeleton -> discogs track fuzzy hit -> synthetic result."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        discogs_cache.search_releases_by_title = AsyncMock(return_value=[])
        discogs_cache.search_tracks_by_title = AsyncMock(
            return_value=[
                {
                    "id": 555,
                    "title": "Back, Baby",
                    "artist": "Jessica Pratt",
                    "score": 0.92,
                },
            ]
        )
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            song="Back Baby",
            raw_message="Back Baby",
            include_external_caches=True,
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache=discogs_cache,
                mb_pg=mb_pg,
            )

        assert response.external_source == "discogs"
        assert len(response.results) == 1
        item = response.results[0].library_item
        assert item.artist == "Jessica Pratt"
        assert item.title == "Back, Baby"
        discogs_cache.search_artists_by_name.assert_not_called()
        discogs_cache.search_releases_by_title.assert_not_called()
        discogs_cache.search_tracks_by_title.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_artist_takes_precedence_over_album_when_both_present(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """When the request supplies both artist AND album, artist branch runs first."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(
            return_value=[{"id": 1, "name": "Stereolab", "score": 0.9}]
        )
        discogs_cache.search_releases_by_title = AsyncMock(return_value=[])
        discogs_cache.search_tracks_by_title = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            artist="Stereolab",
            album="Aluminum Tunes",
            raw_message="Stereolab Aluminum Tunes",
            include_external_caches=True,
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache=discogs_cache,
                mb_pg=mb_pg,
            )

        assert response.external_source == "discogs"
        discogs_cache.search_artists_by_name.assert_awaited_once()
        discogs_cache.search_releases_by_title.assert_not_called()
        discogs_cache.search_tracks_by_title.assert_not_called()
