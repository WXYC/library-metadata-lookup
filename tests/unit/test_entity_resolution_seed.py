"""Unit tests for ``seed_identities`` V/A guard (LML#385).

Mock-store layer: verifies the ``is_compilation_artist`` filter inside
``seed_identities`` skips V/A bucket entries and lets real artists through.
The keep-set / false-positive control rows (``Epic Soundtracks``,
``The 27 Various``) lock the wxyc-etl 0.5.0 anchored matcher's behavior at
the call site that LML#379's audit found polluted.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from entity.store import Identity
from scripts.entity_resolution.__main__ import seed_identities


@pytest.fixture
def mock_store():
    """Mock EntityStore whose ``upsert_identity`` records every call."""
    store = AsyncMock()
    store.upsert_identity = AsyncMock(
        side_effect=lambda library_name, **_: Identity(id=1, library_name=library_name)
    )
    return store


class TestSeedIdentitiesCompilationGuard:
    @pytest.mark.asyncio
    async def test_skips_v_a_filings_and_seeds_real_artists(self, mock_store):
        """The four-artist mix from LML#385's issue body.

        ``Hermanos Gutiérrez`` is a canonical WXYC artist; ``Epic Soundtracks``
        is the false-positive control row from the wxyc-etl 0.5.0 keep-set
        (real Swell Maps drummer, name ends in "Soundtracks" but is not a V/A
        filing).
        """
        artists = [
            "Soundtracks - A",
            "Various Artists - Rock - C",
            "Hermanos Gutiérrez",
            "Epic Soundtracks",
        ]

        seeded = await seed_identities(mock_store, artists)

        assert seeded == 2
        seeded_names = [
            call.kwargs["library_name"] for call in mock_store.upsert_identity.await_args_list
        ]
        assert seeded_names == ["Hermanos Gutiérrez", "Epic Soundtracks"]

    @pytest.mark.asyncio
    async def test_logs_skipped_count(self, mock_store, caplog):
        """``seed.skipped_compilation count=N`` lands at INFO each run."""
        artists = ["Soundtracks - A", "Various Artists - Rock - C", "Hermanos Gutiérrez"]

        with caplog.at_level(logging.INFO, logger="scripts.entity_resolution.__main__"):
            await seed_identities(mock_store, artists)

        assert any(
            "seed.skipped_compilation count=2" in record.message for record in caplog.records
        ), f"expected count=2 in logs, got: {[r.message for r in caplog.records]}"

    @pytest.mark.asyncio
    async def test_canonical_fixture_artists_all_seeded(self, mock_store):
        """No-compilation snapshot: every artist seeds, count=0."""
        artists = ["Juana Molina", "Jessica Pratt", "Chuquimamani-Condori"]

        seeded = await seed_identities(mock_store, artists)

        assert seeded == 3
        assert mock_store.upsert_identity.await_count == 3

    @pytest.mark.asyncio
    async def test_empty_input(self, mock_store):
        """Empty snapshot: returns 0, no upserts."""
        seeded = await seed_identities(mock_store, [])

        assert seeded == 0
        mock_store.upsert_identity.assert_not_called()

    @pytest.mark.asyncio
    async def test_upsert_returning_none_not_counted(self, mock_store):
        """``upsert_identity`` returning None (row already existed) does not bump ``seeded``."""
        mock_store.upsert_identity = AsyncMock(return_value=None)

        seeded = await seed_identities(mock_store, ["Stereolab", "Cat Power"])

        assert seeded == 0
        assert mock_store.upsert_identity.await_count == 2
