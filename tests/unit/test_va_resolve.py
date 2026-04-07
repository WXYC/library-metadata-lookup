"""Tests for the resolution orchestrator."""

from unittest.mock import AsyncMock, patch

import pytest

from scripts.va_disambiguate.models import FlowsheetEntry, ResolutionResult
from scripts.va_disambiguate.resolve import resolve_flowsheet_entry

# Confidence thresholds from the plan
DEFAULT_THRESHOLD = 0.70


@pytest.fixture
def library_index():
    return {"koo nimo": 10, "sessa": 20, "cat power": 30}


@pytest.fixture
def pool():
    return AsyncMock()


class TestResolveFlowsheetEntry:
    @pytest.mark.asyncio
    async def test_embedded_artist_takes_priority(self, pool, library_index):
        """Strategy 1 (embedded parse) should run before Discogs lookup."""
        entry = FlowsheetEntry(
            id=1, artist_name="Various Artists- Sessa",
            song_title="Pequena Vertigem", release_title="Pequena Vertigem de Amor",
        )
        result = await resolve_flowsheet_entry(entry, pool, library_index)
        assert result is not None
        assert result.resolved_artist == "Sessa"
        assert result.strategy == "embedded"
        assert result.confidence == 0.95
        assert result.library_code_id == 20
        # Discogs should NOT have been called
        pool.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_exact_match_strategy(self, pool, library_index):
        """Strategy 2: exact track+release title match."""
        entry = FlowsheetEntry(
            id=1, artist_name="V/A",
            song_title="odo akosomo", release_title="Vintage Palmwine",
        )
        with patch("scripts.va_disambiguate.resolve.resolve_track_artist_exact") as mock_exact:
            mock_exact.return_value = [
                {"artist_name": "Koo Nimo", "track_title": "Odo Akosomo",
                 "release_title": "Vintage Palmwine", "sim": 1.0}
            ]
            result = await resolve_flowsheet_entry(entry, pool, library_index)

        assert result is not None
        assert result.resolved_artist == "Koo Nimo"
        assert result.strategy == "exact"
        assert result.confidence == 0.90
        assert result.library_code_id == 10

    @pytest.mark.asyncio
    async def test_fuzzy_match_strategy(self, pool, library_index):
        """Strategy 3: fuzzy match when exact fails."""
        entry = FlowsheetEntry(
            id=1, artist_name="V/A",
            song_title="odo akosomo", release_title="Vintage Palmwine",
        )
        with (
            patch("scripts.va_disambiguate.resolve.resolve_track_artist_exact") as mock_exact,
            patch("scripts.va_disambiguate.resolve.resolve_track_artist_fuzzy") as mock_fuzzy,
        ):
            mock_exact.return_value = []
            mock_fuzzy.return_value = [
                {"artist_name": "Koo Nimo", "track_title": "Odo Akosomo",
                 "release_title": "Vintage Palmwine", "song_sim": 0.85, "release_sim": 0.90}
            ]
            result = await resolve_flowsheet_entry(entry, pool, library_index)

        assert result is not None
        assert result.resolved_artist == "Koo Nimo"
        assert result.strategy == "fuzzy"
        assert result.confidence > DEFAULT_THRESHOLD

    @pytest.mark.asyncio
    async def test_song_only_strategy(self, pool, library_index):
        """Strategy 4: song-only when no release title."""
        entry = FlowsheetEntry(
            id=1, artist_name="V/A",
            song_title="Cross Bones Style", release_title=None,
        )
        with (
            patch("scripts.va_disambiguate.resolve.resolve_track_artist_exact") as mock_exact,
            patch("scripts.va_disambiguate.resolve.resolve_track_artist_song_only") as mock_song,
        ):
            mock_exact.return_value = []
            mock_song.return_value = [
                {"artist_name": "Cat Power", "track_title": "Cross Bones Style",
                 "release_title": "Moon Pix"}
            ]
            result = await resolve_flowsheet_entry(
                entry, pool, library_index, confidence_threshold=0.50
            )

        assert result is not None
        assert result.resolved_artist == "Cat Power"
        assert result.strategy == "song_only"

    @pytest.mark.asyncio
    async def test_no_song_title_returns_none(self, pool, library_index):
        """Entry with no song title cannot be resolved."""
        entry = FlowsheetEntry(id=1, artist_name="V/A", song_title=None)
        result = await resolve_flowsheet_entry(entry, pool, library_index)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_song_title_returns_none(self, pool, library_index):
        entry = FlowsheetEntry(id=1, artist_name="V/A", song_title="")
        result = await resolve_flowsheet_entry(entry, pool, library_index)
        assert result is None

    @pytest.mark.asyncio
    async def test_below_threshold_returns_none(self, pool, library_index):
        """Fuzzy match below confidence threshold returns None."""
        entry = FlowsheetEntry(
            id=1, artist_name="V/A",
            song_title="some track", release_title="some album",
        )
        with (
            patch("scripts.va_disambiguate.resolve.resolve_track_artist_exact") as mock_exact,
            patch("scripts.va_disambiguate.resolve.resolve_track_artist_fuzzy") as mock_fuzzy,
            patch("scripts.va_disambiguate.resolve.resolve_track_artist_song_only") as mock_song,
        ):
            mock_exact.return_value = []
            mock_fuzzy.return_value = [
                {"artist_name": "Someone", "track_title": "something",
                 "release_title": "something", "song_sim": 0.3, "release_sim": 0.3}
            ]
            mock_song.return_value = []
            result = await resolve_flowsheet_entry(
                entry, pool, library_index, confidence_threshold=0.70
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_all_strategies_fail_returns_none(self, pool, library_index):
        entry = FlowsheetEntry(
            id=1, artist_name="V/A",
            song_title="totally unknown", release_title="mystery album",
        )
        with (
            patch("scripts.va_disambiguate.resolve.resolve_track_artist_exact") as mock_exact,
            patch("scripts.va_disambiguate.resolve.resolve_track_artist_fuzzy") as mock_fuzzy,
            patch("scripts.va_disambiguate.resolve.resolve_track_artist_song_only") as mock_song,
        ):
            mock_exact.return_value = []
            mock_fuzzy.return_value = []
            mock_song.return_value = []
            result = await resolve_flowsheet_entry(entry, pool, library_index)

        assert result is None
