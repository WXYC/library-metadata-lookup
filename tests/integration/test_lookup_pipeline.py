"""Integration tests for the lookup pipeline with real LibraryDB."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from discogs.matching import normalize_artist_for_validation
from discogs.models import (
    DiscogsSearchResponse,
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackItem,
    TrackReleasesResponse,
)


class TestLookupPipeline:
    @pytest.mark.asyncio
    async def test_direct_match(self, app_client):
        """Artist + album direct match."""
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
        assert body["search_type"] == "direct"

    @pytest.mark.asyncio
    async def test_artist_only(self, app_client):
        """Artist-only search returns that artist's albums."""
        resp = await app_client.post(
            "/api/v1/lookup",
            json={
                "artist": "Juana Molina",
                "raw_message": "Juana Molina",
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
                "artist": "Stereolab",
                "album": "Dots and Loops",
                "raw_message": "Stereolab - Dots and Loops",
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


class TestLookupResultAttributesProjection:
    """LML#158: result-quality attributes on the /api/v1/lookup transaction.

    Mirrors the LML#213 cache_stats projection pattern. After the orchestrator
    runs its full pipeline, it must attach `lookup.results_count` and
    `lookup.match_type` to the active Sentry transaction so trace explorer can
    diagnose "right artist, wrong album" failures (the Underscores → fishmonger
    class) without per-caller instrumentation.
    """

    @pytest.mark.asyncio
    async def test_direct_match_projects_results_count_and_match_type(self, app_client):
        """A direct artist+album match projects the count and match_type onto the txn."""
        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("lookup.orchestrator.sentry_sdk.get_current_scope", return_value=mock_scope):
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
        calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        # Existing projection still fires.
        assert "lml.lookup.extended" in calls
        assert "lml.lookup.warm_cache" in calls
        # New result-quality attributes.
        assert calls["lookup.results_count"] == len(body["results"])
        assert calls["lookup.match_type"] == body["search_type"]
        # match_type is a SearchType slug, not a python-repr.
        assert isinstance(calls["lookup.match_type"], str)
        assert calls["lookup.match_type"] in {
            "direct",
            "fallback",
            "alternative",
            "compilation",
            "song_as_artist",
            "none",
        }

    @pytest.mark.asyncio
    async def test_no_results_projects_zero_count(self, app_client):
        """A miss still projects results_count=0 and match_type, so traces filter cleanly."""
        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("lookup.orchestrator.sentry_sdk.get_current_scope", return_value=mock_scope):
            resp = await app_client.post(
                "/api/v1/lookup",
                json={
                    "artist": "ZZZNONEXISTENT",
                    "raw_message": "ZZZNONEXISTENT",
                },
            )

        assert resp.status_code == 200
        calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert calls["lookup.results_count"] == 0
        # Even on miss, match_type is set (likely "none" or a fallback slug).
        assert "lookup.match_type" in calls
        assert isinstance(calls["lookup.match_type"], str)

    @pytest.mark.asyncio
    async def test_projection_no_op_without_active_transaction(self, app_client):
        """No active Sentry transaction -> projection is a no-op (no crash)."""
        mock_scope = Mock()
        mock_scope.transaction = None

        with patch("lookup.orchestrator.sentry_sdk.get_current_scope", return_value=mock_scope):
            resp = await app_client.post(
                "/api/v1/lookup",
                json={
                    "artist": "Stereolab",
                    "album": "Dots and Loops",
                    "raw_message": "Stereolab - Dots and Loops",
                },
            )

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_projection_failure_does_not_break_request(self, app_client):
        """A raising set_data must not break the lookup. Observability is not load-bearing."""
        mock_transaction = Mock()
        mock_transaction.set_data = Mock(side_effect=RuntimeError("boom"))
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("lookup.orchestrator.sentry_sdk.get_current_scope", return_value=mock_scope):
            resp = await app_client.post(
                "/api/v1/lookup",
                json={
                    "artist": "Stereolab",
                    "album": "Dots and Loops",
                    "raw_message": "Stereolab - Dots and Loops",
                },
            )

        assert resp.status_code == 200


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


class TestSongTitleRanking:
    """When several library candidates by the same artist all contain the
    requested track, the album whose title matches the song name must rank
    first — regardless of which album the upstream Discogs track-lookup
    happened to return first."""

    @pytest.mark.asyncio
    async def test_title_album_beats_compilation_when_both_contain_song(self, library_db):
        """'Meet Me in the City' by Junior Kimbrough should return the album
        of that name (KI 6/4) ahead of the 'You Better Run' essentials comp
        (KI 6/5), even when Discogs returned the comp first.

        Seed data:
        - (51752, "Meet Me in the City", "Junior Kimbrough")
        - (51753, "You Better Run (The essential Junior Kimbrough)", "Junior Kimbrough")

        Both albums contain "Meet Me in the City"; both pass track validation.
        Sorting by `albums[0]` from the upstream Discogs track lookup is
        non-deterministic when several releases tie on track-title similarity
        in the PG cache, so the title-matching album must be promoted by the
        song key.
        """
        from wxyc_fastapi.observability import init_cache_stats

        from lookup.models import LookupRequest
        from lookup.orchestrator import perform_lookup
        from tests.conftest import make_lml_telemetry
        from tests.factories import make_discogs_result

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

        meet_me_discogs = make_discogs_result(
            release_id=8001, album="Meet Me in the City", artist="Junior Kimbrough"
        )
        you_better_run_discogs = make_discogs_result(
            release_id=8002,
            album="You Better Run: The Essential Junior Kimbrough",
            artist="Junior Kimbrough",
        )

        async def mock_search(request):
            album = (request.album if hasattr(request, "album") else "") or ""
            album_lower = album.lower()
            if "meet me in the city" in album_lower:
                return DiscogsSearchResponse(results=[meet_me_discogs])
            if "you better run" in album_lower or "essential" in album_lower:
                return DiscogsSearchResponse(results=[you_better_run_discogs])
            return DiscogsSearchResponse(results=[])

        mock_service.search = AsyncMock(side_effect=mock_search)
        # The track is genuinely on both releases — neither should be filtered.
        mock_service.validate_track_on_release = AsyncMock(return_value=True)
        mock_service.get_release = AsyncMock(return_value=None)
        mock_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Meet Me in the City",
                artist="Junior Kimbrough",
                releases=[],
                total=0,
                cached=False,
            )
        )

        request = LookupRequest(
            artist="Junior Kimbrough",
            song="Meet Me in the City",
            raw_message="Meet Me in the City Junior Kimbrough",
        )

        # Bug-triggering Discogs order: comp first, title-match album second.
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[
                ("Junior Kimbrough", "You Better Run: The Essential Junior Kimbrough"),
                ("Junior Kimbrough", "Meet Me in the City"),
            ],
        ):
            response = await perform_lookup(
                request,
                library_db,
                mock_service,
                make_lml_telemetry(),
            )

        titles = [r.library_item.title for r in response.results]
        assert titles[0] == "Meet Me in the City", (
            f"Title-matching album should rank first; got: {titles}"
        )
        assert "You Better Run (The essential Junior Kimbrough)" in titles, (
            f"Comp should still appear (track is on it too); got: {titles}"
        )
        assert response.song_not_found is False


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
        from wxyc_fastapi.observability import init_cache_stats

        from discogs.service import DiscogsService
        from lookup.models import LookupRequest
        from lookup.orchestrator import perform_lookup
        from tests.conftest import make_lml_telemetry

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
            artist_lower = normalize_artist_for_validation(artist)
            for item in release.tracklist:
                if track_lower in item.title.lower() or item.title.lower() in track_lower:
                    release_artist = normalize_artist_for_validation(release.artist)
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

        response = await perform_lookup(
            request,
            library_db,
            mock_service,
            make_lml_telemetry(),
        )

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
        from wxyc_fastapi.observability import init_cache_stats

        from discogs.service import DiscogsService
        from lookup.models import LookupRequest
        from lookup.orchestrator import perform_lookup
        from tests.conftest import make_lml_telemetry

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

        response = await perform_lookup(
            request,
            library_db,
            mock_service,
            make_lml_telemetry(),
        )

        assert response.found_on_compilation is True, (
            "Track on VA compilation should set found_on_compilation=True"
        )
        assert len(response.results) >= 1
        titles = [r.library_item.title for r in response.results]
        assert "Now That's What I Call Music 47" in titles


class TestFoundOnCompilationArtwork:
    """LML#684: a found_on_compilation result must carry the matched Discogs
    release's artwork even when the Step-4 artist-floor re-search rejects the
    candidate — the systematic failure for a non-Various-Artists trio /
    collaboration credit.

    Repro (prod, 2026-06-23): ``Orcutt Shelley Miller`` / ``Orcutt Shelley
    Miller`` / ``A Star Is Born``. The library row is filed under "Bill Orcutt"
    (release 34993109 credits the full trio), so the floor re-search can't clear
    it. The release was already validated during the compilation search, so its
    artwork must be trust-bound rather than dropped (release_id=0 / empty url).
    """

    @pytest.mark.asyncio
    async def test_trio_compilation_result_carries_release_artwork(self, library_db):
        """Seed row (70001, "Orcutt-Shelley-Miller", "Bill Orcutt"). The
        album-title fallback in search_compilations_for_track locates it via
        release 34993109; the floor re-search rejects the trio credit; the
        carried, validated release's artwork is bound."""
        from wxyc_fastapi.observability import init_cache_stats

        from discogs.service import DiscogsService
        from lookup.models import LookupRequest
        from lookup.orchestrator import perform_lookup
        from tests.conftest import make_lml_telemetry

        init_cache_stats()

        mock_service = AsyncMock(spec=DiscogsService)
        mock_service.cache_service = None

        # The two artist-scoped probes find nothing — no single canonical entity
        # exists for the trio (the motivating shape for the album-title fallback).
        mock_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="A Star Is Born", artist="Orcutt Shelley Miller", releases=[], total=0
            )
        )
        # The album-title fallback surfaces the trio release. Discogs does NOT
        # classify it as a compilation; its credit is the full trio.
        mock_service.search_releases_by_album_title = AsyncMock(
            return_value=TrackReleasesResponse(
                track="",
                artist="",
                releases=[
                    ReleaseInfo(
                        album="Orcutt-Shelley-Miller",
                        artist="Bill Orcutt, Chris Corsano, Sarah Louise",
                        release_id=34993109,
                        release_url="https://www.discogs.com/release/34993109",
                        is_compilation=False,
                    ),
                ],
                total=1,
                cached=False,
            )
        )
        # The track validates on the trio release.
        mock_service.validate_track_on_release = AsyncMock(return_value=True)
        # Step-4 artist-floor re-search returns nothing usable -> None.
        mock_service.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
        # The carried release's own cover (trust-bind path).
        mock_service.get_release = AsyncMock(
            return_value=ReleaseMetadataResponse(
                release_id=34993109,
                title="Orcutt-Shelley-Miller",
                artist="Bill Orcutt, Chris Corsano, Sarah Louise",
                release_url="https://www.discogs.com/release/34993109",
                artwork_url="https://i.discogs.com/osm.jpg",
            )
        )

        request = LookupRequest(
            artist="Orcutt Shelley Miller",
            album="Orcutt Shelley Miller",
            song="A Star Is Born",
            raw_message="Orcutt Shelley Miller - Orcutt Shelley Miller - A Star Is Born",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request,
                library_db,
                mock_service,
                make_lml_telemetry(),
            )

        assert response.found_on_compilation is True, (
            "Trio compilation hit should set found_on_compilation=True"
        )
        assert len(response.results) >= 1
        top = response.results[0]
        assert top.library_item.title == "Orcutt-Shelley-Miller"
        assert top.artwork is not None, "found_on_compilation result must carry artwork (#684)"
        assert top.artwork.release_id == 34993109
        assert top.artwork.artwork_url == "https://i.discogs.com/osm.jpg"


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
        from wxyc_fastapi.observability import init_cache_stats

        from lookup.models import LookupRequest
        from lookup.orchestrator import perform_lookup
        from tests.conftest import make_lml_telemetry

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
            response = await perform_lookup(
                request,
                library_db,
                mock_service,
                make_lml_telemetry(),
            )

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


class TestPromoteAlbumFromCachedTrackData:
    """Cache-driven safety net for the artist-only fallback.

    When the artist-only FTS5 fallback returns N albums by ID order, none of
    which can be validated against the requested track, but the local Discogs
    PG cache holds the answer — promote the cache-known album.

    Reproduces "bucky skank by lee scratch perry": fallback returned 5 unrelated
    Lee Perry albums, validator couldn't confirm Bucky Skank on any, while the
    cache knew it was on "Live at Maritime Hall" all along.
    """

    @pytest.mark.asyncio
    async def test_cache_promotes_song_bearing_album_over_unrelated_fallback(self, library_db):
        """Library has 6 Lee Perry albums; FTS-by-ID returns 5 that don't have
        "Bucky Skank"; cache says it's on "Live at Maritime Hall" → promotion
        should surface Maritime Hall and clear `song_not_found`.
        """
        from wxyc_fastapi.observability import init_cache_stats

        from discogs.models import (
            DiscogsSearchResponse,
            ReleaseInfo,
            TrackReleasesResponse,
        )
        from lookup.models import LookupRequest
        from lookup.orchestrator import perform_lookup
        from tests.conftest import make_lml_telemetry

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

        # Compilation/artist-keyword Discogs lookups return nothing (mirrors prod).
        mock_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Bucky Skank",
                artist="Lee 'Scratch' Perry",
                releases=[],
                total=0,
                cached=False,
            )
        )

        # Per-result Discogs.search returns nothing for the validator either, so
        # `filter_results_by_track_validation` cannot confirm any fallback album.
        mock_service.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
        mock_service.validate_track_on_release = AsyncMock(return_value=False)
        mock_service.get_release = AsyncMock(return_value=None)

        # The PG cache holds the answer.
        mock_service.cache_service = AsyncMock()
        mock_service.cache_service.search_releases_by_track = AsyncMock(
            return_value=[
                ReleaseInfo(
                    album="Lee Scratch Perry Live At Maritime Hall",
                    artist="Lee 'Scratch' Perry",
                    release_id=2865555,
                    release_url="https://www.discogs.com/release/2865555",
                    is_compilation=False,
                ),
            ]
        )

        request = LookupRequest(
            artist="Lee 'Scratch' Perry",
            song="Bucky Skank",
            raw_message="bucky skank by lee scratch perry",
        )

        # Patch the upstream track→releases lookup to return nothing — this is
        # exactly the production failure mode the safety net is designed for.
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request,
                library_db,
                mock_service,
                make_lml_telemetry(),
            )

        titles = [r.library_item.title for r in response.results]
        assert "Live at Maritime Hall" in titles, (
            f"Cache-known song-bearing album should be promoted; got: {titles}"
        )
        # Promotion replaces the unrelated fallback set
        for noise in ("Chicken Scratch", "Arkology", "Dub Fire"):
            assert noise not in titles, (
                f"{noise!r} doesn't contain the song; should be replaced, got: {titles}"
            )
        assert response.song_not_found is False, (
            "song_not_found should clear once a track-bearing album is confirmed"
        )

    @pytest.mark.asyncio
    async def test_no_promotion_when_cache_has_no_matching_release(self, library_db):
        """Cache empty → existing artist-fallback behavior is preserved."""
        from wxyc_fastapi.observability import init_cache_stats

        from discogs.models import DiscogsSearchResponse, TrackReleasesResponse
        from lookup.models import LookupRequest
        from lookup.orchestrator import perform_lookup
        from tests.conftest import make_lml_telemetry

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
        mock_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Bucky Skank",
                artist="Lee 'Scratch' Perry",
                releases=[],
                total=0,
                cached=False,
            )
        )
        mock_service.search = AsyncMock(return_value=DiscogsSearchResponse(results=[]))
        mock_service.validate_track_on_release = AsyncMock(return_value=False)
        mock_service.get_release = AsyncMock(return_value=None)

        # Cache has no answer either
        mock_service.cache_service = AsyncMock()
        mock_service.cache_service.search_releases_by_track = AsyncMock(return_value=[])

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
                request,
                library_db,
                mock_service,
                make_lml_telemetry(),
            )

        # Without a cache answer we keep the artist-fallback behavior:
        # the user still gets albums by the artist, and song_not_found stays True.
        assert response.song_not_found is True
        assert len(response.results) >= 1
        for r in response.results:
            assert "Perry" in (r.library_item.artist or "")
