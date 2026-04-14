"""Unit tests for entity deduplication by shared Wikidata QID."""

from unittest.mock import AsyncMock

import pytest

from scripts.entity_resolution.dedup import EntityDeduplicator
from scripts.entity_resolution.store import Identity


@pytest.fixture
def mock_pg():
    """Mock PgSource for entity store queries."""
    pg = AsyncMock()
    pg.fetchall = AsyncMock(return_value=[])
    pg.execute = AsyncMock(return_value="UPDATE 0")
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

        # Should have executed UPDATE for the primary identity and DELETE for dupes
        calls = [str(c) for c in mock_pg.execute.call_args_list]
        update_call = [c for c in calls if "UPDATE" in c]
        delete_call = [c for c in calls if "DELETE" in c]
        assert len(update_call) >= 1, "Expected UPDATE for merged identity"
        assert len(delete_call) >= 1, "Expected DELETE for duplicate identity"
