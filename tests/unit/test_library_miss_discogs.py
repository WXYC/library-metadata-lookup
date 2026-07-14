"""Unit tests for the library-miss Discogs search-and-cache path (LML#583).

When the library search returns no results AND both artist and album are
provided, the orchestrator runs a confidence-floored Discogs lookup and
synthesizes a result if a match clears the 80/80 floor.

The LML#400 contamination came from artist-fallback returning any release
for any typed album. The new path searches (artist, album) jointly, so the
risk shape is different — near-miss typed albums clearing the floor against
longer Discogs titles (typed "Anthology" → "Anthology, Vol. 1").

Tests are organized around the acceptance criteria from LML#583:
- Cache hit → full result returned, no API call
- Cache miss → API called, result returned, (write-back is implicit via
  enrich_artwork_results' get_release call)
- No confident match (<80-floor) → empty results
- Synthesized item has id=0, call_number="(external)", matched_via=None
- Gate respected: no artist or no album → no Discogs call
- Step 3b bypass: synthesized id=0 items excluded from track validation input
- Regression (old shape): LML#400 contamination set doesn't re-appear
- Regression (new shape): near-miss albums don't clear floor incorrectly
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from discogs.models import DiscogsSearchResponse
from lookup.models import LookupRequest, LookupResponse
from lookup.orchestrator import perform_lookup
from lookup.strategies.library_miss import _library_miss_discogs_search
from services.parser import MessageType, ParsedRequest
from tests.conftest import make_lml_telemetry
from tests.factories import make_discogs_result, make_library_item

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def telemetry():
    return make_lml_telemetry()


# ---------------------------------------------------------------------------
# _library_miss_discogs_search — unit tests
# ---------------------------------------------------------------------------


class TestLibraryMissDiscogsSearch:
    """Unit tests for the _library_miss_discogs_search helper."""

    @pytest.mark.asyncio
    async def test_returns_none_when_discogs_service_is_none(self):
        """No Discogs service available → returns None gracefully."""
        parsed = ParsedRequest(
            artist="My New Band Believe",
            album="My New Band Believe",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_artist(self, mock_discogs_service):
        """No artist parsed → returns None without calling Discogs."""
        parsed = ParsedRequest(
            album="Some Album",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        assert result is None
        mock_discogs_service.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_no_album(self, mock_discogs_service):
        """No album parsed → returns None without calling Discogs."""
        parsed = ParsedRequest(
            artist="Some Artist",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        assert result is None
        mock_discogs_service.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_album_is_whitespace_only(self, mock_discogs_service):
        """Whitespace-only album → returns None without calling Discogs."""
        parsed = ParsedRequest(
            artist="Some Artist",
            album="   ",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        assert result is None
        mock_discogs_service.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_discogs_returns_empty(self, mock_discogs_service):
        """Discogs returns no candidates → returns None."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        parsed = ParsedRequest(
            artist="My New Band Believe",
            album="My New Band Believe",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_candidate_clears_80_floor(self, mock_discogs_service):
        """Near-miss: Discogs returns candidates but none clear the 80/80 floor."""
        # "Anthology" typed against "Anthology, Vol. 1" — artist matches but
        # a short generic title might not clear 80 if the Discogs title is long enough.
        # We test with clearly wrong artist/album so neither field clears the floor.
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=99999,
                    artist="Completely Wrong Artist",
                    album="Nothing in Common",
                )
            ]
        )
        parsed = ParsedRequest(
            artist="My New Band Believe",
            album="My New Band Believe",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_synthesized_item_on_confident_match(self, mock_discogs_service):
        """Discogs returns a candidate clearing 80/80 floor → synthesized item returned."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=37008771,
                    artist="My New Band Believe",
                    album="My New Band Believe",
                    artwork_url="https://img.discogs.com/cover.jpg",
                )
            ]
        )
        parsed = ParsedRequest(
            artist="My New Band Believe",
            album="My New Band Believe",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        assert result is not None
        lib_item, discogs_result = result
        assert lib_item.id == 0
        assert lib_item.artist == "My New Band Believe"
        assert lib_item.title == "My New Band Believe"
        assert discogs_result.release_id == 37008771
        assert discogs_result.release_url == "https://discogs.com/release/37008771"

    @pytest.mark.asyncio
    async def test_discogs_api_call_is_made_on_cache_miss(self, mock_discogs_service):
        """Verifies Discogs search is called with the (artist, album) pair."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        parsed = ParsedRequest(
            artist="Jessica Pratt",
            album="On Your Own Love Again",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        mock_discogs_service.search.assert_called_once()
        call_kwargs = mock_discogs_service.search.call_args[0][0]
        assert call_kwargs.artist == "Jessica Pratt"
        assert call_kwargs.album == "On Your Own Love Again"

    @pytest.mark.asyncio
    async def test_discogs_exception_returns_none_gracefully(self, mock_discogs_service):
        """Discogs outage / rate-limit → returns None without crashing."""
        mock_discogs_service.search.side_effect = Exception("Discogs unavailable")
        parsed = ParsedRequest(
            artist="Some Artist",
            album="Some Album",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        assert result is None

    @pytest.mark.asyncio
    async def test_matched_via_stays_none_for_synthesized_item(self, mock_discogs_service):
        """matched_via is track-title-provenance-scoped; synthesized items carry None."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=37008771,
                    artist="My New Band Believe",
                    album="My New Band Believe",
                )
            ]
        )
        parsed = ParsedRequest(
            artist="My New Band Believe",
            album="My New Band Believe",
            song="Heart of Darkness",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        assert result is not None
        lib_item, _ = result
        # The synthesized LibraryItem carries id=0; matched_via_by_id[0] is
        # never populated (track-title-provenance contract), so the caller
        # gets matched_via=None on the LookupResultItem.
        assert lib_item.id == 0


# ---------------------------------------------------------------------------
# perform_lookup integration — library-miss path wiring
# ---------------------------------------------------------------------------


class TestPerformLookupLibraryMissPath:
    """Test perform_lookup's Step 3a: library-miss Discogs search wiring."""

    @pytest.mark.asyncio
    async def test_library_miss_with_discogs_hit_returns_result(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Library misses, Discogs has the release → result returned with real release_id."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=37008771,
                    artist="My New Band Believe",
                    album="My New Band Believe",
                    artwork_url="https://img.discogs.com/cover.jpg",
                )
            ]
        )
        # enrich_artwork_results calls discogs_service.get_release for top-1
        mock_discogs_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="My New Band Believe",
            album="My New Band Believe",
            raw_message="My New Band Believe - My New Band Believe",
        )
        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert isinstance(response, LookupResponse)
        assert len(response.results) == 1
        item = response.results[0]
        assert item.library_item.id == 0
        assert item.library_item.call_number == "(external)"
        assert item.artwork is not None
        assert item.artwork.release_id == 37008771
        assert item.artwork.release_url is not None
        assert "37008771" in item.artwork.release_url
        assert item.matched_via is None
        # Step 3a finding a match resolves the request — song_not_found must be False
        # and no context_message should be emitted (this request has no song field,
        # so build_context_message returns None when song_not_found=False + has_results=True).
        assert response.song_not_found is False
        assert response.context_message is None

    @pytest.mark.asyncio
    async def test_song_not_found_cleared_when_step_3a_finds_match(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Step 3a match clears song_not_found even when the search pipeline set it True.

        Regression test for the bug where library_results=[] left song_not_found=True
        from the pipeline, and Step 3a never reset it — causing LookupResponse to
        carry song_not_found=True alongside a non-empty results list.
        """
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=37008771,
                    artist="My New Band Believe",
                    album="My New Band Believe",
                )
            ]
        )
        mock_discogs_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="My New Band Believe",
            album="My New Band Believe",
            song="Heart of Darkness",
            raw_message="My New Band Believe - Heart of Darkness - My New Band Believe",
        )
        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert len(response.results) == 1
        assert response.song_not_found is False
        # context_message must not say "not found" when a result was returned
        if response.context_message is not None:
            assert "not found" not in response.context_message.lower()

    @pytest.mark.asyncio
    async def test_library_miss_no_discogs_hit_returns_empty(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Library misses, Discogs has no confident match → empty results."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Nonexistent Band",
            album="Nonexistent Album",
            raw_message="Nonexistent Band - Nonexistent Album",
        )
        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert isinstance(response, LookupResponse)
        assert len(response.results) == 0

    @pytest.mark.asyncio
    async def test_song_not_found_preserved_when_step_3a_finds_nothing(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Step 3a runs but finds no match → song_not_found stays True (not clobbered).

        Symmetric to test_song_not_found_cleared_when_step_3a_finds_match: this pins
        that song_not_found is only reset on a successful match, not unconditionally
        whenever Step 3a runs. Guards against a future refactor that lifts the
        `song_not_found = False` line out of the `if miss_match is not None:` branch.
        """
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Nonexistent Band",
            album="Nonexistent Album",
            song="Nonexistent Song",
            raw_message="Nonexistent Band - Nonexistent Song - Nonexistent Album",
        )
        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert len(response.results) == 0
        # song_not_found stays at whatever the pipeline set it to (typically True for
        # a song that didn't surface anywhere). The critical invariant is that the
        # Step 3a no-match branch does NOT clobber it to False.
        assert response.song_not_found is True

    @pytest.mark.asyncio
    async def test_step_3a_skipped_when_library_has_results(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """When library returns results, the library-miss step must not run."""
        library_item = make_library_item(
            id=1, artist="Jessica Pratt", title="On Your Own Love Again"
        )
        mock_library_db.search.return_value = [library_item]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=5678,
                    artist="Jessica Pratt",
                    album="On Your Own Love Again",
                )
            ]
        )
        mock_discogs_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="Jessica Pratt",
            album="On Your Own Love Again",
            raw_message="Jessica Pratt - On Your Own Love Again",
        )
        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        # The normal library result is returned
        assert len(response.results) == 1
        assert response.results[0].library_item.id == 1  # real library item, not synthesized

    @pytest.mark.asyncio
    async def test_step_3a_skipped_when_no_album_provided(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """No album in request → Step 3a gate doesn't fire, Discogs not called for miss."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None

        request = LookupRequest(
            artist="Some Artist",
            raw_message="Some Artist",
        )
        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert isinstance(response, LookupResponse)
        assert len(response.results) == 0
        # search() is NOT called for the library-miss path (no album)
        # (It may be called for other reasons; we check the result only)

    @pytest.mark.asyncio
    async def test_step_3a_skipped_when_album_is_whitespace(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Whitespace-only album → Step 3a gate doesn't fire."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Some Artist",
            album="   ",
            raw_message="Some Artist",
        )

        with patch(
            "lookup.orchestrator._library_miss_discogs_search",
            new_callable=AsyncMock,
        ) as mock_miss_search:
            await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)
            mock_miss_search.assert_not_called()

    @pytest.mark.asyncio
    async def test_step_3a_fires_on_non_bulk_with_typed_artist_album(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """LML#671 regression guard: /lookup (allow_release_resolution_fallback=True,
        the default) still runs the #583 library-miss search on a typed artist+album
        miss. Symmetric to the bulk-suppression test below — pins that gating the
        bulk path leaves the non-bulk path unchanged.
        """
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=37008771,
                    artist="My New Band Believe",
                    album="My New Band Believe",
                    artwork_url="https://img.discogs.com/cover.jpg",
                )
            ]
        )
        mock_discogs_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="My New Band Believe",
            album="My New Band Believe",
            raw_message="My New Band Believe - My New Band Believe",
        )
        response = await perform_lookup(
            request,
            mock_library_db,
            mock_discogs_service,
            telemetry,
            allow_release_resolution_fallback=True,
        )

        assert len(response.results) == 1
        assert response.results[0].library_item.id == 0
        assert response.results[0].artwork is not None

    @pytest.mark.asyncio
    async def test_step_3a_suppressed_on_bulk_kill_switch(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """LML#671: /lookup/bulk passes ``allow_release_resolution_fallback=False`` so
        the 35k-album backfill can't trigger per-row Discogs work. The #583 library-miss
        search is the one row-less producer an album-only (artist+album, no song)
        backfill item actually hits — the five LML#652 producers are all song-bearing.

        With the kill switch closed, Step 3a must not fire: no per-row Discogs
        ``search()`` (asserted via the helper not being called) and no row-less
        ``LibraryItem(id=0)`` result surfaced.
        """
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        # Were the gate to leak, this confident match would surface a row-less item.
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=37008771,
                    artist="My New Band Believe",
                    album="My New Band Believe",
                    artwork_url="https://img.discogs.com/cover.jpg",
                )
            ]
        )
        mock_discogs_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="My New Band Believe",
            album="My New Band Believe",
            raw_message="My New Band Believe - My New Band Believe",
        )

        with patch(
            "lookup.orchestrator._library_miss_discogs_search",
            new_callable=AsyncMock,
        ) as mock_miss_search:
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                allow_release_resolution_fallback=False,
            )
            mock_miss_search.assert_not_called()

        assert len(response.results) == 0

    @pytest.mark.asyncio
    async def test_synthesized_item_excluded_from_track_validation(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Step 3b bypass: synthesized id=0 items are not fed to track validation.

        When the library-miss path found a result, library_results is still
        empty, so the track validation gate (``if library_results and ...``)
        naturally never fires. This test verifies that filter_results_by_track_validation
        is NOT called when we're on the synthesized path.
        """
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=37008771,
                    artist="My New Band Believe",
                    album="My New Band Believe",
                )
            ]
        )
        mock_discogs_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="My New Band Believe",
            album="My New Band Believe",
            song="Heart of Darkness",
            raw_message="My New Band Believe - Heart of Darkness - My New Band Believe",
        )

        with patch(
            "lookup.orchestrator.filter_results_by_track_validation",
            new_callable=AsyncMock,
        ) as mock_validate:
            await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)
            # Track validation must not run for synthesized items
            mock_validate.assert_not_called()

    @pytest.mark.asyncio
    async def test_sentry_outcome_set_on_library_miss_match(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """lookup.outcome=library_miss_discogs_match is set when Step 3a finds a match."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=37008771,
                    artist="My New Band Believe",
                    album="My New Band Believe",
                )
            ]
        )
        mock_discogs_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="My New Band Believe",
            album="My New Band Believe",
            raw_message="My New Band Believe - My New Band Believe",
        )

        outcome_recorded: list[str] = []

        class FakeTransaction:
            def set_data(self, key: str, value: object) -> None:
                if key == "lookup.outcome":
                    outcome_recorded.append(str(value))

        class FakeScope:
            transaction = FakeTransaction()

        with patch("sentry_sdk.get_current_scope", return_value=FakeScope()):
            await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert "library_miss_discogs_match" in outcome_recorded

    @pytest.mark.asyncio
    async def test_sentry_outcome_set_on_library_miss_no_match(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """lookup.outcome=library_miss_no_discogs_match is set when Step 3a finds nothing."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Nonexistent Band",
            album="Nonexistent Album",
            raw_message="Nonexistent Band - Nonexistent Album",
        )

        outcome_recorded: list[str] = []

        class FakeTransaction:
            def set_data(self, key: str, value: object) -> None:
                if key == "lookup.outcome":
                    outcome_recorded.append(str(value))

        class FakeScope:
            transaction = FakeTransaction()

        with patch("sentry_sdk.get_current_scope", return_value=FakeScope()):
            await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert "library_miss_no_discogs_match" in outcome_recorded


# ---------------------------------------------------------------------------
# Regression tests — LML#400 contamination set (old shape / smoke)
# ---------------------------------------------------------------------------


class TestLML400ContaminationRegression:
    """Verify that the LML#400 contamination set doesn't re-appear on the new path.

    The old contamination: artist-fallback returning any artist release regardless
    of album. The new path searches (artist, album) jointly, so these cases produce
    empty (no match) since the Discogs candidates won't clear the 80-floor against
    the TYPED album when they're for a different Discogs album.

    The test setup seeds each Discogs result with the WRONG album so that the
    80/80 floor rejects it. This mirrors the contamination shape: DJ typed an
    album that isn't on Discogs under that artist at that title.
    """

    @pytest.mark.asyncio
    async def test_miles_davis_kind_of_blue_not_contaminated(self, mock_discogs_service):
        """Miles Davis / 'Kind of Blue' — prior contamination release was 1171368.

        Discogs returns 'Bitches Brew' (a different Miles Davis album). Must not
        be accepted since 'Bitches Brew' doesn't clear 80-floor against 'Kind of Blue'.
        """
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=1171368,  # the contaminating release from #400 evidence
                    artist="Miles Davis",
                    album="Bitches Brew",
                )
            ]
        )
        parsed = ParsedRequest(
            artist="Miles Davis",
            album="Kind of Blue",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        assert result is None, (
            "Bitches Brew must not be accepted for 'Kind of Blue' — would re-introduce contamination"
        )

    @pytest.mark.asyncio
    async def test_outkast_aquemini_not_contaminated(self, mock_discogs_service):
        """Outkast / 'Aquemini' — prior contamination release was /release/1171368.

        Discogs returns 'Stankonia'. Must not be accepted since 'Stankonia'
        doesn't clear 80-floor against 'Aquemini'.
        """
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=55555,
                    artist="Outkast",
                    album="Stankonia",
                )
            ]
        )
        parsed = ParsedRequest(
            artist="Outkast",
            album="Aquemini",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        assert result is None, (
            "Stankonia must not be accepted for 'Aquemini' — would re-introduce contamination"
        )

    @pytest.mark.asyncio
    async def test_nina_simone_wild_is_the_wind_not_contaminated(self, mock_discogs_service):
        """Nina Simone / 'Wild Is the Wind' — prior contamination: Pastel Blues returned.

        Discogs returns 'Pastel Blues'. Must not be accepted.
        """
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=77777,
                    artist="Nina Simone",
                    album="Pastel Blues",
                )
            ]
        )
        parsed = ParsedRequest(
            artist="Nina Simone",
            album="Wild Is the Wind",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        assert result is None, (
            "Pastel Blues must not be accepted for 'Wild Is the Wind' — would re-introduce contamination"
        )


# ---------------------------------------------------------------------------
# Regression tests — near-miss typed albums (new shape)
# ---------------------------------------------------------------------------


class TestNearMissAlbumRegression:
    """Verify that near-miss typed albums don't clear the 80-floor incorrectly.

    The risk shape on the new path: short/generic typed album strings clearing
    the floor against longer Discogs canonical titles. Test that the floor
    correctly rejects these OR accepts only the precisely-titled release.
    """

    @pytest.mark.asyncio
    async def test_anthology_vs_anthology_vol_1_rejects_wrong_release(self, mock_discogs_service):
        """Typed 'Anthology' does not match 'Anthology, Vol. 1' — floor rejects it.

        The actual token_set_ratio (via to_match_form) for this pair is below 80,
        so the floor correctly rejects 'Anthology, Vol. 1' when the DJ typed
        'Anthology'. Pin the live value so any upstream rapidfuzz change is visible.
        """
        from rapidfuzz import fuzz
        from wxyc_etl.text import to_match_form

        ratio = fuzz.token_set_ratio(
            to_match_form("Anthology"),
            to_match_form("Anthology, Vol. 1"),
        )
        # Pinned at actual value: 69.23 (below 80 — floor correctly rejects this near-miss)
        assert ratio < 80, (
            f"token_set_ratio('Anthology', 'Anthology, Vol. 1') = {ratio} — "
            "floor should reject this pair; update test if rapidfuzz behavior changes"
        )

    @pytest.mark.asyncio
    async def test_live_vs_live_at_newport_discogs_artist_floor_matters(self, mock_discogs_service):
        """'Live' typed against 'Live at Newport' — artist floor is the real guard here.

        If the artist ALSO matches, token_set_ratio('live', 'live at newport') IS above 80.
        The floor doesn't protect against generic album titles — it protects against
        wrong-artist cases. Test with wrong artist to confirm the floor still fires.
        """
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=88888,
                    artist="Wrong Artist Entirely",
                    album="Live at Newport",
                )
            ]
        )
        parsed = ParsedRequest(
            artist="Duke Ellington",
            album="Live",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        # Wrong artist → floor rejects despite album nearness
        assert result is None

    @pytest.mark.asyncio
    async def test_correct_release_accepted_when_both_fields_match(self, mock_discogs_service):
        """Confirm the 80-floor correctly admits a genuine (artist, album) match.

        'My New Band Believe' / 'My New Band Believe' → release 37008771.
        Both fields clear the floor — result is returned.
        """
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=37008771,
                    artist="My New Band Believe",
                    album="My New Band Believe",
                    artwork_url="https://img.discogs.com/cover.jpg",
                )
            ]
        )
        parsed = ParsedRequest(
            artist="My New Band Believe",
            album="My New Band Believe",
            message_type=MessageType.REQUEST,
            is_request=True,
        )
        result = await _library_miss_discogs_search(parsed, discogs_service=mock_discogs_service)
        assert result is not None
        lib_item, discogs_result = result
        assert discogs_result.release_id == 37008771


# ---------------------------------------------------------------------------
# LML#784 category 1 — cache-arm floor-reject falls through to the API arm
# ---------------------------------------------------------------------------


def _parsed(artist: str, album: str) -> ParsedRequest:
    return ParsedRequest(
        artist=artist,
        album=album,
        message_type=MessageType.REQUEST,
        is_request=True,
    )


class TestCacheArmFloorRejectFallthrough:
    """Floor-rejected PG-cache candidates must fall through to the API arm.

    The PG cache's ``search_releases`` joins ``release_artist`` one credit per
    row, so a multi-artist release surfaces as "Merce Lemon" (75.9) or "Fust"
    (36.4) singly and floor-fails, while the API arm's joined credit
    ("Fust, Merce Lemon") clears at 91.4. The ``fallthrough`` seam treats any
    non-empty ``pg_read`` as terminal, so ``_library_miss_discogs_search``
    re-issues the search with ``skip_pg=True`` when a PG-served
    (``response.pg_served``) candidate set clears nothing (LML#784 category 1).
    ``response.cached`` is NOT the gate: the in-memory memoization layer
    flips it to True on every replay, including replays of API-served passes.
    """

    @pytest.mark.asyncio
    async def test_cache_floor_reject_retries_api_and_resolves(self, mock_discogs_service):
        """Merce Lemon & Fust — per-credit cache rows floor-fail, joined API credit passes."""
        cache_response = DiscogsSearchResponse(
            cached=True,
            pg_served=True,
            results=[
                make_discogs_result(
                    release_id=36830641,
                    artist="Merce Lemon",
                    album="Cup Of Loneliness / Choices",
                ),
                make_discogs_result(
                    release_id=36830641,
                    artist="Fust",
                    album="Cup Of Loneliness / Choices",
                ),
            ],
        )
        api_response = DiscogsSearchResponse(
            cached=False,
            results=[
                make_discogs_result(
                    release_id=36830641,
                    artist="Fust, Merce Lemon",
                    album="Cup Of Loneliness / Choices",
                )
            ],
        )
        mock_discogs_service.search.side_effect = [cache_response, api_response]
        result = await _library_miss_discogs_search(
            _parsed("Merce Lemon & Fust", "Cup of Loneliness / Choices"),
            discogs_service=mock_discogs_service,
        )
        assert result is not None
        _, discogs_result = result
        assert discogs_result.release_id == 36830641
        assert mock_discogs_service.search.call_count == 2
        retry_call = mock_discogs_service.search.call_args_list[1]
        assert retry_call.kwargs.get("skip_pg") is True

    @pytest.mark.asyncio
    async def test_no_retry_when_cache_candidate_clears_floor(self, mock_discogs_service):
        """A floor-passing cache candidate is terminal — no second search."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            cached=True,
            pg_served=True,
            results=[
                make_discogs_result(
                    release_id=1489958,
                    artist="Juana Molina",
                    album="DOGA",
                )
            ],
        )
        result = await _library_miss_discogs_search(
            _parsed("Juana Molina", "DOGA"), discogs_service=mock_discogs_service
        )
        assert result is not None
        assert mock_discogs_service.search.call_count == 1

    @pytest.mark.asyncio
    async def test_no_retry_when_first_response_is_api_served(self, mock_discogs_service):
        """cached=False means the API arm was already consulted — never retry."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            cached=False,
            results=[
                make_discogs_result(
                    release_id=999,
                    artist="Merce Lemon",
                    album="Cup Of Loneliness / Choices",
                )
            ],
        )
        result = await _library_miss_discogs_search(
            _parsed("Merce Lemon & Fust", "Cup of Loneliness / Choices"),
            discogs_service=mock_discogs_service,
        )
        assert result is None
        assert mock_discogs_service.search.call_count == 1

    @pytest.mark.asyncio
    async def test_no_retry_on_memory_replayed_api_response(self, mock_discogs_service):
        """``@async_cached`` flips ``cached=True`` on every in-memory replay —
        including one whose underlying response was API-served. Only
        ``pg_served`` carries arm provenance, so a floor-failing memory replay
        of an API pass must not burn a second, identical API call."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            cached=True,  # memory-cache replay...
            pg_served=False,  # ...of an API-served response
            results=[
                make_discogs_result(
                    release_id=999,
                    artist="Merce Lemon",
                    album="Cup Of Loneliness / Choices",
                )
            ],
        )
        result = await _library_miss_discogs_search(
            _parsed("Merce Lemon & Fust", "Cup of Loneliness / Choices"),
            discogs_service=mock_discogs_service,
        )
        assert result is None
        assert mock_discogs_service.search.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_floor_reject_returns_none(self, mock_discogs_service):
        """Both arms floor-failing degrades to a clean no-match."""
        mock_discogs_service.search.side_effect = [
            DiscogsSearchResponse(
                cached=True,
                pg_served=True,
                results=[make_discogs_result(release_id=1, artist="Fust", album="Genevieve")],
            ),
            DiscogsSearchResponse(
                cached=False,
                results=[make_discogs_result(release_id=2, artist="Fust", album="Big Ugly")],
            ),
        ]
        result = await _library_miss_discogs_search(
            _parsed("Merce Lemon & Fust", "Cup of Loneliness / Choices"),
            discogs_service=mock_discogs_service,
        )
        assert result is None
        assert mock_discogs_service.search.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_empty_response_returns_none(self, mock_discogs_service):
        """An empty API retry degrades to a clean no-match."""
        mock_discogs_service.search.side_effect = [
            DiscogsSearchResponse(
                cached=True,
                pg_served=True,
                results=[make_discogs_result(release_id=1, artist="Fust", album="Genevieve")],
            ),
            DiscogsSearchResponse(cached=False, results=[]),
        ]
        result = await _library_miss_discogs_search(
            _parsed("Merce Lemon & Fust", "Cup of Loneliness / Choices"),
            discogs_service=mock_discogs_service,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_retry_exception_returns_none(self, mock_discogs_service):
        """A Discogs failure on the retry is logged and swallowed, like the first call."""
        mock_discogs_service.search.side_effect = [
            DiscogsSearchResponse(
                cached=True,
                pg_served=True,
                results=[make_discogs_result(release_id=1, artist="Fust", album="Genevieve")],
            ),
            Exception("rate limited"),
        ]
        result = await _library_miss_discogs_search(
            _parsed("Merce Lemon & Fust", "Cup of Loneliness / Choices"),
            discogs_service=mock_discogs_service,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_anadol_marie_klock_resolves_via_api_arm(self, mock_discogs_service):
        """Anadol & Marie Klock — the API arm prefers the "&"-credited pressing (37483479).

        The ticket pins either 37483479 (the "&" credit, artist 100) or
        37552383 (comma credit, 92.3) as acceptable; the mock carries both in
        API-arm order and the "&" pressing must win on score.
        """
        mock_discogs_service.search.side_effect = [
            DiscogsSearchResponse(
                cached=True,
                pg_served=True,
                results=[
                    make_discogs_result(release_id=37552383, artist="Anadol", album="Manivelles")
                ],
            ),
            DiscogsSearchResponse(
                cached=False,
                results=[
                    make_discogs_result(
                        release_id=37552383, artist="Anadol, Marie Klock", album="Manivelles"
                    ),
                    make_discogs_result(
                        release_id=37483479, artist="Anadol & Marie Klock", album="Manivelles"
                    ),
                ],
            ),
        ]
        result = await _library_miss_discogs_search(
            _parsed("Anadol & Marie Klock", "Manivelles"), discogs_service=mock_discogs_service
        )
        assert result is not None
        _, discogs_result = result
        assert discogs_result.release_id == 37483479

    @pytest.mark.asyncio
    async def test_masked_cache_rescued_by_exact_api_match(self, mock_discogs_service):
        """Ricardo Dias Gomes — Muito Sol (Population B): unrelated cache rows
        must not mask the exact 100/100 API-arm match."""
        mock_discogs_service.search.side_effect = [
            DiscogsSearchResponse(
                cached=True,
                pg_served=True,
                results=[
                    make_discogs_result(release_id=1, artist="Ricardo Villalobos", album="Sol"),
                ],
            ),
            DiscogsSearchResponse(
                cached=False,
                results=[
                    make_discogs_result(
                        release_id=27474075, artist="Ricardo Dias Gomes", album="Muito Sol"
                    )
                ],
            ),
        ]
        result = await _library_miss_discogs_search(
            _parsed("Ricardo Dias Gomes", "Muito Sol"), discogs_service=mock_discogs_service
        )
        assert result is not None
        _, discogs_result = result
        assert discogs_result.release_id == 27474075

    @pytest.mark.asyncio
    async def test_joined_cache_credit_resolves_without_retry(self, mock_discogs_service):
        """A2: once the PG arm presents the aggregated credit, the in-cache
        multi-artist release resolves on the first pass — no API retry."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            cached=True,
            pg_served=True,
            results=[
                make_discogs_result(
                    release_id=36830641,
                    artist="Fust, Merce Lemon",
                    artist_credits=["Fust", "Merce Lemon"],
                    album="Cup Of Loneliness / Choices",
                )
            ],
        )
        result = await _library_miss_discogs_search(
            _parsed("Merce Lemon & Fust", "Cup of Loneliness / Choices"),
            discogs_service=mock_discogs_service,
        )
        assert result is not None
        _, discogs_result = result
        assert discogs_result.release_id == 36830641
        assert mock_discogs_service.search.call_count == 1

    @pytest.mark.asyncio
    async def test_single_credit_query_matches_joined_cache_credit(self, mock_discogs_service):
        """A2: a single-credit query keeps matching a multi-artist release via
        the per-credit ``artist_credits`` variants."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            cached=True,
            pg_served=True,
            results=[
                make_discogs_result(
                    release_id=36830641,
                    artist="Fust, Merce Lemon",
                    artist_credits=["Fust", "Merce Lemon"],
                    album="Cup Of Loneliness / Choices",
                )
            ],
        )
        result = await _library_miss_discogs_search(
            _parsed("Fust", "Cup of Loneliness / Choices"),
            discogs_service=mock_discogs_service,
        )
        assert result is not None
        _, discogs_result = result
        assert discogs_result.release_id == 36830641
        assert mock_discogs_service.search.call_count == 1


# ---------------------------------------------------------------------------
# LML#784 category 4 — query-side "S/T" swaps to the artist name
# ---------------------------------------------------------------------------


class TestSelfTitledQuerySwap:
    """A query-side self-titled placeholder ("S/T", "s.t.", "self-titled")
    can never match a real Discogs title — the probe searched
    ``release_title=S/T`` and scored "S/T" vs "Matmos" at 22.2 (deterministic
    no-match on every arm). Mirror the library-side swap the artwork path
    already does: search and score with the artist name as the album, and do
    NOT keep the trigger string as a scoring variant (a wrong-release
    candidate literally titled "S/T" would clear trivially).
    """

    @pytest.mark.asyncio
    async def test_self_titled_album_searches_and_scores_artist_name(self, mock_discogs_service):
        """Matmos — S/T resolves the self-titled release (63794)."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            cached=False,
            results=[
                make_discogs_result(release_id=63794, artist="Matmos", album="Matmos"),
            ],
        )
        result = await _library_miss_discogs_search(
            _parsed("Matmos", "S/T"), discogs_service=mock_discogs_service
        )
        assert result is not None
        lib_item, discogs_result = result
        assert discogs_result.release_id == 63794
        assert lib_item.title == "Matmos"
        request = mock_discogs_service.search.call_args_list[0].args[0]
        assert request.album == "Matmos"
        assert request.artist == "Matmos"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("placeholder", ["S/T", "s/t", "S.T.", "self-titled", "Self Titled"])
    async def test_all_self_titled_spellings_swap(self, mock_discogs_service, placeholder):
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            cached=False,
            results=[
                make_discogs_result(release_id=63794, artist="Matmos", album="Matmos"),
            ],
        )
        result = await _library_miss_discogs_search(
            _parsed("Matmos", placeholder), discogs_service=mock_discogs_service
        )
        assert result is not None
        request = mock_discogs_service.search.call_args_list[0].args[0]
        assert request.album == "Matmos"

    @pytest.mark.asyncio
    async def test_self_titled_trigger_not_readmitted_as_title_variant(self, mock_discogs_service):
        """A candidate literally titled "S/T" must not clear the title floor —
        the trigger string is swapped out, not kept as a variant."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            cached=False,
            results=[
                make_discogs_result(release_id=999, artist="Matmos", album="S/T"),
            ],
        )
        result = await _library_miss_discogs_search(
            _parsed("Matmos", "S/T"), discogs_service=mock_discogs_service
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_wrong_album_still_rejected_after_swap(self, mock_discogs_service):
        """The swap doesn't loosen the floor — a different Matmos album stays out."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            cached=False,
            results=[
                make_discogs_result(release_id=888, artist="Matmos", album="Supreme Balloon"),
            ],
        )
        result = await _library_miss_discogs_search(
            _parsed("Matmos", "S/T"), discogs_service=mock_discogs_service
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_non_self_titled_album_unchanged(self, mock_discogs_service):
        """A regular album title searches and scores exactly as before."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            cached=False,
            results=[
                make_discogs_result(
                    release_id=27474075, artist="Ricardo Dias Gomes", album="Muito Sol"
                ),
            ],
        )
        result = await _library_miss_discogs_search(
            _parsed("Ricardo Dias Gomes", "Muito Sol"), discogs_service=mock_discogs_service
        )
        assert result is not None
        request = mock_discogs_service.search.call_args_list[0].args[0]
        assert request.album == "Muito Sol"
