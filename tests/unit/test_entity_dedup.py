"""Unit tests for entity deduplication by shared Wikidata QID."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from entity.store import Identity
from scripts.entity_resolution.dedup import EntityDeduplicator
from tests.unit.conftest import make_mock_conn


@pytest.fixture
def mock_pg():
    """Mock PgSource exposing pool-level methods plus ``acquire()`` for transactions.

    ``find_duplicate_groups`` uses the legacy pool-level ``fetchall``/``execute``;
    ``merge_group`` (WXYC#380) uses ``acquire()`` + ``conn.transaction()``.
    """
    pg = AsyncMock()
    pg.fetchall = AsyncMock(return_value=[])
    pg.execute = AsyncMock(return_value="UPDATE 0")

    conn = make_mock_conn()
    conn.execute = AsyncMock(return_value="UPDATE 0")

    acq_ctx = MagicMock()
    acq_ctx.__aenter__ = AsyncMock(return_value=conn)
    acq_ctx.__aexit__ = AsyncMock(return_value=False)
    pg.acquire = MagicMock(return_value=acq_ctx)
    pg._mock_conn = conn
    return pg


@pytest.fixture
def dedup(mock_pg):
    return EntityDeduplicator(mock_pg)


class TestFindDuplicateQids:
    @pytest.mark.asyncio
    async def test_finds_duplicates(self, dedup, mock_pg):
        """find_duplicate_groups returns groups of identities sharing a QID."""
        mock_pg.fetchall = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "library_name": "Autechre",
                    "discogs_artist_id": 12,
                    "wikidata_qid": "Q378288",
                    "musicbrainz_artist_id": None,
                    "spotify_artist_id": "abc",
                    "apple_music_artist_id": None,
                    "bandcamp_id": None,
                    "reconciliation_status": "reconciled",
                },
                {
                    "id": 2,
                    "library_name": "autechre",
                    "discogs_artist_id": None,
                    "wikidata_qid": "Q378288",
                    "musicbrainz_artist_id": "mbid-1",
                    "spotify_artist_id": None,
                    "apple_music_artist_id": "apple-1",
                    "bandcamp_id": None,
                    "reconciliation_status": "reconciled",
                },
            ]
        )
        groups = await dedup.find_duplicate_groups()
        assert len(groups) == 1
        qid, identities = groups[0]
        assert qid == "Q378288"
        assert len(identities) == 2

    @pytest.mark.asyncio
    async def test_no_duplicates(self, dedup, mock_pg):
        """find_duplicate_groups returns empty list when no shared QIDs."""
        mock_pg.fetchall = AsyncMock(return_value=[])
        groups = await dedup.find_duplicate_groups()
        assert groups == []


class TestMergeIdentities:
    @pytest.mark.asyncio
    async def test_merge_preserves_all_ids(self, dedup, mock_pg):
        """merge_group keeps the first identity and merges external IDs from all."""
        identities = [
            Identity(
                id=1,
                library_name="Autechre",
                discogs_artist_id=12,
                wikidata_qid="Q378288",
                spotify_artist_id="abc",
                reconciliation_status="reconciled",
            ),
            Identity(
                id=2,
                library_name="autechre",
                musicbrainz_artist_id="mbid-1",
                wikidata_qid="Q378288",
                apple_music_artist_id="apple-1",
                reconciliation_status="reconciled",
            ),
        ]
        await dedup.merge_group("Q378288", identities)

        # Should have executed UPDATE for the primary identity and DELETE for dupes,
        # all on the connection acquired from the pool (not the pool-level execute).
        conn = mock_pg._mock_conn
        calls = [str(c) for c in conn.execute.call_args_list]
        update_call = [c for c in calls if "UPDATE entity.identity" in c]
        delete_call = [c for c in calls if "DELETE" in c]
        assert len(update_call) >= 1, "Expected UPDATE for merged identity"
        assert len(delete_call) >= 1, "Expected DELETE for duplicate identity"

    @pytest.mark.asyncio
    async def test_merge_group_runs_in_single_transaction(self, dedup, mock_pg):
        """merge_group acquires one connection and wraps all statements in a transaction.

        Without the wrapper, each pg.execute() autocommits and a mid-loop
        interruption leaves some duplicates reassigned-but-not-deleted (eventually
        consistent next pass, but harder to reason about). See WXYC/library-metadata-lookup#380.
        """
        identities = [
            Identity(id=1, library_name="A", wikidata_qid="Q1", reconciliation_status="reconciled"),
            Identity(id=2, library_name="a", wikidata_qid="Q1", reconciliation_status="reconciled"),
            Identity(
                id=3, library_name="A.", wikidata_qid="Q1", reconciliation_status="reconciled"
            ),
        ]
        await dedup.merge_group("Q1", identities)

        # One acquire, one transaction, all statements on the same conn.
        mock_pg.acquire.assert_called_once()
        conn = mock_pg._mock_conn
        conn.transaction.assert_called_once()
        conn._mock_tx_ctx.__aenter__.assert_awaited_once()
        conn._mock_tx_ctx.__aexit__.assert_awaited_once()
        # Statements should land on the connection, not the pool wrapper.
        assert conn.execute.await_count >= 1 + 2 * 2  # UPDATE + 2×(REASSIGN+DELETE)
