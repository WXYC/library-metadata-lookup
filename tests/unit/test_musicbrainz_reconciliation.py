"""Unit tests for MusicBrainz identity resolution."""

from unittest.mock import AsyncMock

import pytest

from scripts.entity_resolution.musicbrainz import MusicBrainzReconciler


@pytest.fixture
def mock_mb_pg():
    """Mock PgSource for musicbrainz-cache."""
    pg = AsyncMock()
    pg.fetchall = AsyncMock(return_value=[])
    pg.fetchone = AsyncMock(return_value=None)
    return pg


@pytest.fixture
def mock_wikidata_pg():
    """Mock PgSource for wikidata-cache (P434 bridge)."""
    pg = AsyncMock()
    pg.fetchall = AsyncMock(return_value=[])
    return pg


@pytest.fixture
def reconciler(mock_mb_pg, mock_wikidata_pg):
    return MusicBrainzReconciler(mb_pg=mock_mb_pg, wikidata_pg=mock_wikidata_pg)


class TestQidToMbidViaP434:
    @pytest.mark.asyncio
    async def test_bridge_lookup(self, reconciler, mock_wikidata_pg, mock_mb_pg):
        """QID -> MBID via P434 wikidata-cache bridge, confirmed in MB cache."""
        mock_wikidata_pg.fetchall = AsyncMock(
            return_value=[
                {"qid": "Q378288", "musicbrainz_artist_id": "410c9baf-5469-44f6-9852-826524b80c61"}
            ]
        )
        mock_mb_pg.fetchall = AsyncMock(
            return_value=[{"gid": "410c9baf-5469-44f6-9852-826524b80c61", "name": "Autechre"}]
        )
        result = await reconciler.resolve_from_qids({"Q378288"})
        assert "Q378288" in result
        assert result["Q378288"] == "410c9baf-5469-44f6-9852-826524b80c61"


class TestDirectNameMatch:
    @pytest.mark.asyncio
    async def test_name_match(self, reconciler, mock_mb_pg):
        """Direct name match against mb_artist + mb_artist_alias."""
        mock_mb_pg.fetchall = AsyncMock(
            return_value=[
                {
                    "name": "autechre",
                    "gid": "410c9baf-5469-44f6-9852-826524b80c61",
                }
            ]
        )
        result = await reconciler.resolve_from_names(["Autechre"])
        assert "Autechre" in result
        assert result["Autechre"] == "410c9baf-5469-44f6-9852-826524b80c61"

    @pytest.mark.asyncio
    async def test_no_match(self, reconciler, mock_mb_pg):
        """Returns empty dict when no name match found."""
        mock_mb_pg.fetchall = AsyncMock(return_value=[])
        result = await reconciler.resolve_from_names(["ZZZNONEXISTENT"])
        assert result == {}


class TestGracefulDegradation:
    @pytest.mark.asyncio
    async def test_no_mb_cache_skips(self):
        """When mb_pg is None, all resolution methods return empty results."""
        reconciler = MusicBrainzReconciler(mb_pg=None, wikidata_pg=None)
        result_qid = await reconciler.resolve_from_qids({"Q378288"})
        result_name = await reconciler.resolve_from_names(["Autechre"])
        assert result_qid == {}
        assert result_name == {}
