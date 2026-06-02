"""Unit tests for `EntityStore.bulk_resolve_library_names`.

Batched form of the three-leg fall-through (#276): exact / `LOWER()` /
canonical. Each leg is one `WHERE … = ANY($1::text[])` against the
still-missing residual, so a 1,000-name batch is 1-3 PG round-trips
regardless of cardinality (vs. ~1k-3k for the per-name loop). The batch
is the artist-search-alias plan PR 2's Phase 1.

Returns a dict keyed by the **input** name (not the stored
`library_name`) so the composer can preserve input order without a
reverse index.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from entity.store import EntityStore


@pytest.fixture
def mock_pg():
    pg = AsyncMock()
    pg.fetchall = AsyncMock(return_value=[])
    pg.fetchone = AsyncMock(return_value=None)
    pg.execute = AsyncMock(return_value="OK")
    return pg


@pytest.fixture
def store(mock_pg):
    return EntityStore(mock_pg)


def _row(library_name: str, **overrides):
    return {
        "id": 1,
        "library_name": library_name,
        "discogs_artist_id": None,
        "wikidata_qid": None,
        "musicbrainz_artist_id": None,
        "spotify_artist_id": None,
        "apple_music_artist_id": None,
        "bandcamp_id": None,
        "reconciliation_status": "reconciled",
        **overrides,
    }


class TestBulkResolveLibraryNames:
    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_dict_without_querying(self, store, mock_pg):
        """No names → zero PG round-trips. The composer's miss path stays cheap."""
        result = await store.bulk_resolve_library_names([])
        assert result == {}
        assert mock_pg.fetchall.await_count == 0

    @pytest.mark.asyncio
    async def test_single_leg_when_all_inputs_hit_exact(self, store, mock_pg):
        """All names resolved on leg 1 → one PG round-trip total."""
        mock_pg.fetchall = AsyncMock(
            return_value=[
                _row("Stereolab", id=1, discogs_artist_id=2154),
                _row("Juana Molina", id=2, discogs_artist_id=305253),
            ]
        )
        result = await store.bulk_resolve_library_names(["Stereolab", "Juana Molina"])
        assert mock_pg.fetchall.await_count == 1
        assert set(result.keys()) == {"Stereolab", "Juana Molina"}
        assert result["Stereolab"].discogs_artist_id == 2154
        assert result["Juana Molina"].discogs_artist_id == 305253

    @pytest.mark.asyncio
    async def test_two_legs_when_some_inputs_drop_to_case_insensitive(self, store, mock_pg):
        """Leg 1 hits the verbatim row; leg 2 catches a case-drift residual."""
        mock_pg.fetchall = AsyncMock(
            side_effect=[
                # Leg 1: only the verbatim-cased input hits.
                [_row("Stereolab", id=1, discogs_artist_id=2154)],
                # Leg 2: LOWER('stereolab') = LOWER(stored 'Stereolab') hits.
                [_row("Stereolab", id=1, discogs_artist_id=2154)],
            ]
        )
        # Map of input → stored canonical/lower used by the leg 2 bind. Tests
        # the contract that the dict is keyed by INPUT name, not stored shape.
        result = await store.bulk_resolve_library_names(["Stereolab", "stereolab"])
        assert mock_pg.fetchall.await_count == 2
        assert set(result.keys()) == {"Stereolab", "stereolab"}
        assert result["Stereolab"].discogs_artist_id == 2154
        assert result["stereolab"].discogs_artist_id == 2154

    @pytest.mark.asyncio
    async def test_three_legs_when_canonical_form_is_needed(self, store, mock_pg):
        """Diacritic-bearing input falls through to leg 3 (canonical)."""
        mock_pg.fetchall = AsyncMock(
            side_effect=[
                [],  # leg 1: no exact match
                [],  # leg 2: LOWER both sides still has the diacritic
                # Leg 3: canonical 'nilufer yanya' matches the stored canonical row.
                [_row("nilufer yanya", id=7, discogs_artist_id=5499521)],
            ]
        )
        result = await store.bulk_resolve_library_names(["Nilüfer Yanya"])
        assert mock_pg.fetchall.await_count == 3
        assert "Nilüfer Yanya" in result
        assert result["Nilüfer Yanya"].discogs_artist_id == 5499521

    @pytest.mark.asyncio
    async def test_missing_names_absent_from_dict(self, store, mock_pg):
        """Names that miss every leg are simply absent — never NULL-valued."""
        mock_pg.fetchall = AsyncMock(
            side_effect=[
                [_row("Stereolab", id=1, discogs_artist_id=2154)],
                [],
                [],
            ]
        )
        result = await store.bulk_resolve_library_names(["Stereolab", "Nobody Knows"])
        assert "Stereolab" in result
        assert "Nobody Knows" not in result

    @pytest.mark.asyncio
    async def test_residual_only_passed_to_subsequent_legs(self, store, mock_pg):
        """Leg 2 must receive only the names leg 1 missed, not the full input.

        This is the bandwidth saver: a 1000-name batch with a 60% leg-1 hit
        rate sends 400 (not 1000) names to the LOWER leg.
        """
        mock_pg.fetchall = AsyncMock(
            side_effect=[
                [_row("Stereolab", id=1, discogs_artist_id=2154)],
                [_row("Juana Molina", id=2, discogs_artist_id=305253)],
                [],
            ]
        )
        await store.bulk_resolve_library_names(["Stereolab", "juana molina"])
        # The leg-2 bind is the second positional arg to fetchall (after the SQL).
        leg_2_bind = mock_pg.fetchall.await_args_list[1][0][1]
        assert "Stereolab" not in leg_2_bind
        assert "juana molina" in leg_2_bind

    @pytest.mark.asyncio
    async def test_short_circuits_when_no_residual_after_leg_1(self, store, mock_pg):
        """No residual after leg 1 → no leg 2 or 3 queries."""
        mock_pg.fetchall = AsyncMock(
            return_value=[
                _row("Stereolab", id=1, discogs_artist_id=2154),
                _row("Juana Molina", id=2, discogs_artist_id=305253),
            ]
        )
        await store.bulk_resolve_library_names(["Stereolab", "Juana Molina"])
        assert mock_pg.fetchall.await_count == 1

    @pytest.mark.asyncio
    async def test_dict_keyed_by_input_not_stored_library_name(self, store, mock_pg):
        """Case-drift input ('stereolab') resolves to stored row ('Stereolab').

        The returned dict's key must be the INPUT shape so the caller can
        zip its own list of input names against it. If we keyed by stored
        `library_name` instead, the caller would have to maintain its own
        reverse map.
        """
        mock_pg.fetchall = AsyncMock(
            side_effect=[
                [],  # leg 1: 'stereolab' verbatim misses
                [_row("Stereolab", id=1, discogs_artist_id=2154)],  # leg 2: LOWER hits
            ]
        )
        result = await store.bulk_resolve_library_names(["stereolab"])
        # Keyed by input (lowercase), even though the stored row is mixed-case.
        assert "stereolab" in result
        assert "Stereolab" not in result
