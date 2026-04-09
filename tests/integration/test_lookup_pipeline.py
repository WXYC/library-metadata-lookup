"""Integration tests for the lookup pipeline with real LibraryDB."""

from unittest.mock import AsyncMock, patch

import pytest

from core.matching import strip_discogs_suffix
from discogs.models import (
    DiscogsSearchResponse,
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackItem,
    TrackReleasesResponse,
)

pytestmark = pytest.mark.integration


class TestLookupPipeline:
    @pytest.mark.asyncio
    async def test_direct_match(self, app_client):
        """Artist + album direct match."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Queen",
                "album": "The Game",
                "raw_message": "Queen - The Game",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1
        assert body["search_type"] == "direct"

    @pytest.mark.asyncio
    async def test_artist_only(self, app_client):
        """Artist-only search returns that artist's albums."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Radiohead",
                "raw_message": "Radiohead",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1

    @pytest.mark.asyncio
    async def test_artist_only_with_name_substring_collisions(self, app_client):
        """Artist-only search finds results even when other artists/titles share the name.

        Regression test: "Grimes" was returning no results because FTS5 ranked
        entries like "Tiny Grimes", "Henry Grimes", and albums containing "Grimes"
        above the actual "Grimes" artist. With limit=5 applied in SQL before the
        Python artist-prefix filter, all 5 FTS hits were non-matching and got
        filtered out, leaving zero results.
        """
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Grimes",
                "raw_message": "Grimes",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1
        assert all(r["library_item"]["artist"] == "Grimes" for r in body["results"]), (
            f"Expected only Grimes results, got: {[r['library_item']['artist'] for r in body['results']]}"
        )

    @pytest.mark.asyncio
    async def test_no_results(self, app_client):
        """Nonexistent artist returns empty results."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "ZZZNONEXISTENT",
                "raw_message": "ZZZNONEXISTENT",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) == 0

    @pytest.mark.asyncio
    async def test_ambiguous_format(self, app_client):
        """X - Y format triggers alternative interpretation."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Stereolab",
                "album": "Dots and Loops",
                "raw_message": "Stereolab - Dots and Loops",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) >= 1

    @pytest.mark.asyncio
    async def test_song_as_artist(self, app_client):
        """Song parsed as artist name should still find results."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "song": "Laid Back",
                "raw_message": "Laid Back",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # Should find "Laid Back" by Laid Back via SONG_AS_ARTIST strategy
        if body["results"]:
            assert body["search_type"] in ("song_as_artist", "direct")

    @pytest.mark.asyncio
    async def test_response_structure(self, app_client):
        """Response has all expected fields."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Queen",
                "album": "The Game",
                "raw_message": "Queen - The Game",
            },
        )
        body = resp.json()
        assert "results" in body
        assert "search_type" in body
        assert "song_not_found" in body
        assert "found_on_compilation" in body
        assert "context_message" in body

    @pytest.mark.asyncio
    async def test_artist_correction(self, app_client):
        """Misspelled artist should be corrected via fuzzy matching."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Living Color",  # should correct to "Living Colour"
                "raw_message": "Living Color",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # Should have corrected the artist
        if body.get("corrected_artist"):
            assert body["corrected_artist"] == "Living Colour"


class TestSelfTitledAlbumMatching:
    """Test that self-titled albums stored as 'S/t' match correctly."""

    @pytest.mark.asyncio
    async def test_self_titled_album_returned_for_track_search(self, app_client_with_discogs):
        """'Again and Again by The Bird and the Bee' should find the 'S/t' album.

        The library stores the self-titled album as 'S/t'. When Discogs resolves
        the album as 'The Bird and the Bee' (matching the artist name), the album
        title filter should recognize 'S/t' as self-titled and include it.
        """
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[
                ("The Bird and the Bee", "The Bird and the Bee"),
            ],
        ):
            resp = await app_client_with_discogs.post(
                "/api/v1/lookup",
                json={
                    "artist": "The Bird and the Bee",
                    "song": "Again and Again",
                    "raw_message": "Again and Again by The Bird and the Bee",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        titles = [r["library_item"]["title"] for r in body["results"]]
        assert "S/t" in titles, f"Self-titled album 'S/t' should be in results, got: {titles}"
        assert body["search_type"] == "direct"


class TestTrackValidationFiltering:
    """Test that track validation filters false positives from album-resolved results."""

    @pytest.mark.asyncio
    async def test_song_filters_to_correct_album(self, app_client_with_discogs):
        """'Help Me by Joni Mitchell' should return Court and Spark, not the self-titled album.

        The self-titled "Joni Mitchell" album does not contain "Help Me" — it's a
        false positive from album resolution matching the artist name as an album title.
        """
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[
                ("Joni Mitchell", "Court and Spark"),
                ("Joni Mitchell", "Joni Mitchell"),
            ],
        ):
            resp = await app_client_with_discogs.post(
                "/api/v1/lookup",
                json={
                    "artist": "Joni Mitchell",
                    "song": "Help Me",
                    "raw_message": "Play Help Me by Joni Mitchell",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) == 1
        assert body["results"][0]["library_item"]["title"] == "Court and Spark"
        assert body["song_not_found"] is False


class TestQuotedArtistNameValidation:
    """Test that Discogs-formatted quoted artist names don't cause false positives."""

    @pytest.mark.asyncio
    async def test_weird_al_bob_excludes_self_titled(self, library_db):
        """'Bob by Weird Al Yankovic' should NOT return the self-titled album.

        Discogs formats the artist as '"Weird Al" Yankovic' (with quotes). The
        track validation must handle this so 'Poodle Hat' (which contains 'Bob')
        passes validation even when the library doesn't have it. The self-titled
        album (which does NOT contain 'Bob') should be excluded.
        """
        from core.telemetry import RequestTelemetry, init_cache_stats
        from discogs.service import DiscogsService
        from lookup.models import LookupRequest
        from lookup.orchestrator import perform_lookup

        init_cache_stats()

        mock_service = AsyncMock(spec=DiscogsService)
        mock_service.cache_service = None

        # Discogs search_releases_by_track returns Poodle Hat with quoted artist
        from discogs.models import ReleaseInfo, TrackReleasesResponse

        mock_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Bob",
                artist="Weird Al Yankovic",
                releases=[
                    ReleaseInfo(
                        album="Poodle Hat",
                        artist='"Weird Al" Yankovic',
                        release_id=5001,
                        release_url="https://discogs.com/release/5001",
                    ),
                ],
                total=1,
                cached=False,
            )
        )

        # validate_track_on_release: "Bob" IS on Poodle Hat, NOT on self-titled
        poodle_hat_release = ReleaseMetadataResponse(
            release_id=5001,
            title="Poodle Hat",
            artist='"Weird Al" Yankovic',
            release_url="https://discogs.com/release/5001",
            tracklist=[
                TrackItem(position="10", title="Bob"),
            ],
        )
        self_titled_release = ReleaseMetadataResponse(
            release_id=5002,
            title='"Weird Al" Yankovic',
            artist='"Weird Al" Yankovic',
            release_url="https://discogs.com/release/5002",
            tracklist=[
                TrackItem(position="1", title="My Bologna"),
                TrackItem(position="2", title="Another One Rides the Bus"),
            ],
        )

        async def mock_get_release(release_id):
            if release_id == 5001:
                return poodle_hat_release
            if release_id == 5002:
                return self_titled_release
            return None

        mock_service.get_release = AsyncMock(side_effect=mock_get_release)

        async def mock_validate(release_id, track, artist):
            release = await mock_get_release(release_id)
            if release is None:
                return False
            track_lower = track.lower()
            artist_lower = artist.lower().replace('"', "").replace("'", "")
            for item in release.tracklist:
                if track_lower in item.title.lower() or item.title.lower() in track_lower:
                    release_artist = release.artist.lower().replace('"', "").replace("'", "")
                    release_artist = strip_discogs_suffix(release_artist)
                    if artist_lower in release_artist or release_artist in artist_lower:
                        return True
            return False

        mock_service.validate_track_on_release = AsyncMock(side_effect=mock_validate)

        # search returns artwork for whatever album is requested
        mock_service.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))

        request = LookupRequest(
            artist="Weird Al Yankovic",
            song="Bob",
            raw_message='Bob by Weird "Al" Yankovich',
        )

        response = await perform_lookup(request, library_db, mock_service, RequestTelemetry())

        # The self-titled album must NOT be returned as a compilation match.
        # Artist-only fallback results are acceptable (song_not_found=True).
        assert response.found_on_compilation is False, (
            "Self-titled album should not appear as a compilation match for 'Bob'"
        )
        assert response.song_not_found is True, (
            "'Bob' is not on any album in the library, so song_not_found should be True"
        )
        assert response.context_message and "not" in response.context_message.lower(), (
            f"Context should indicate song not found; got: {response.context_message}"
        )


class TestVACompilationTrackSearch:
    """Test that tracks on VA compilations are found via Discogs cross-reference."""

    @pytest.mark.asyncio
    async def test_finds_track_on_va_compilation(self, library_db):
        """Track on a VA compilation should be found when Discogs identifies the release.

        Seed data includes (10, "Now That's What I Call Music 47", "Various Artists").
        When Discogs reports a track is on this compilation, the pipeline should find
        the library entry and return found_on_compilation=True.
        """
        from core.telemetry import RequestTelemetry, init_cache_stats
        from discogs.service import DiscogsService
        from lookup.models import LookupRequest
        from lookup.orchestrator import perform_lookup

        init_cache_stats()

        mock_service = AsyncMock(spec=DiscogsService)
        mock_service.cache_service = None

        # Discogs finds the track on a VA compilation matching the library entry
        mock_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Dancing Queen",
                artist="Chuquimamani-Condori",
                releases=[
                    ReleaseInfo(
                        album="Now That's What I Call Music 47",
                        artist="Various Artists",
                        release_id=9001,
                        release_url="https://discogs.com/release/9001",
                        is_compilation=True,
                    ),
                ],
                total=1,
                cached=False,
            )
        )

        # Validate that the track is on the release
        mock_service.validate_track_on_release = AsyncMock(return_value=True)

        # No artwork
        mock_service.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
        mock_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="Chuquimamani-Condori",
            song="Dancing Queen",
            raw_message="Dancing Queen by Chuquimamani-Condori",
        )

        response = await perform_lookup(request, library_db, mock_service, RequestTelemetry())

        assert response.found_on_compilation is True, (
            "Track on VA compilation should set found_on_compilation=True"
        )
        assert len(response.results) >= 1
        titles = [r.library_item.title for r in response.results]
        assert "Now That's What I Call Music 47" in titles


class TestTrackOnArtistAlbumAndCompilation:
    """Test that tracks on both an artist album and a compilation return both results.

    Bug: "Poison Dart" by "The Bug" is on London Zoo (artist album) AND
    The Sound of Dub (VA compilation). When ARTIST_PLUS_ALBUM falls back to
    artist-only and then TRACK_ON_COMPILATION finds the compilation, the artist
    fallback results should be validated and the artist's own album included.
    """

    @pytest.mark.asyncio
    async def test_artist_album_and_compilation_both_returned(self, library_db):
        """Both London Zoo and The Sound of Dub should appear in results.

        Seed data includes:
        - (26, "London Zoo", "The Bug") - artist's own album
        - (27, "Pressure", "The Bug") - another Bug album (should be excluded)
        - (28, "The Sound of Dub", "Various Artists - Reggae") - compilation
        """
        from core.telemetry import RequestTelemetry, init_cache_stats
        from lookup.models import LookupRequest
        from lookup.orchestrator import perform_lookup

        init_cache_stats()

        mock_service = AsyncMock(
            spec_set=[
                "search_releases_by_track",
                "validate_track_on_release",
                "search",
                "get_release",
                "cache_service",
            ]
        )
        mock_service.cache_service = None

        # Discogs search_releases_by_track returns The Sound of Dub (compilation)
        mock_service.search_releases_by_track = AsyncMock(
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

        # Track validation:
        # - "Poison Dart" IS on London Zoo (release 1395903)
        # - "Poison Dart" IS on The Sound of Dub (release 2308471)
        # - "Poison Dart" is NOT on Pressure (release 9999)
        from tests.factories import make_discogs_result

        london_zoo_discogs = make_discogs_result(
            release_id=1395903, album="London Zoo", artist="The Bug"
        )
        pressure_discogs = make_discogs_result(release_id=9999, album="Pressure", artist="The Bug")

        async def mock_search(request):
            album = request.album if hasattr(request, "album") else ""
            if album and "london" in album.lower():
                return DiscogsSearchResponse(results=[london_zoo_discogs])
            if album and "pressure" in album.lower():
                return DiscogsSearchResponse(results=[pressure_discogs])
            return DiscogsSearchResponse(results=[])

        mock_service.search = AsyncMock(side_effect=mock_search)

        async def mock_validate(release_id, track, artist):
            return release_id in (1395903, 2308471)

        mock_service.validate_track_on_release = AsyncMock(side_effect=mock_validate)
        mock_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="The Bug",
            song="Poison Dart",
            raw_message="poison dart, the bug",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(request, library_db, mock_service, RequestTelemetry())

        titles = [r.library_item.title for r in response.results]
        assert "London Zoo" in titles, (
            f"Artist album 'London Zoo' should be in results, got: {titles}"
        )
        assert "The Sound of Dub" in titles, (
            f"Compilation 'The Sound of Dub' should be in results, got: {titles}"
        )
        assert "Pressure" not in titles, (
            f"'Pressure' (doesn't contain 'Poison Dart') should NOT be in results, got: {titles}"
        )
        assert response.found_on_compilation is True
        # Artist's own album should come before the compilation
        assert titles.index("London Zoo") < titles.index("The Sound of Dub")
