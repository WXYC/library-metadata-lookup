"""Unit tests for Wikidata identity resolution."""

from unittest.mock import AsyncMock

import pytest

from scripts.entity_resolution.wikidata import WikidataReconciler


@pytest.fixture
def mock_sparql():
    """Mock SparqlSource."""
    sparql = AsyncMock()
    sparql.query = AsyncMock(return_value=[])
    sparql.query_batched = AsyncMock(return_value=[])
    sparql.extract_qid = lambda uri: uri.rsplit("/", 1)[-1]
    sparql.binding_value = lambda binding, key: (
        binding.get(key, {}).get("value") if isinstance(binding.get(key), dict) else None
    )
    return sparql


@pytest.fixture
def mock_wikidata_pg():
    """Mock PgSource for wikidata-cache."""
    pg = AsyncMock()
    pg.fetchall = AsyncMock(return_value=[])
    pg.fetchone = AsyncMock(return_value=None)
    return pg


@pytest.fixture
def reconciler(mock_sparql, mock_wikidata_pg):
    return WikidataReconciler(sparql=mock_sparql, wikidata_pg=mock_wikidata_pg)


@pytest.fixture
def reconciler_no_cache(mock_sparql):
    return WikidataReconciler(sparql=mock_sparql, wikidata_pg=None)


class TestDiscogsIdToQidViaCache:
    @pytest.mark.asyncio
    async def test_cache_hit(self, reconciler, mock_wikidata_pg):
        """Discogs ID -> QID lookup succeeds via wikidata-cache discogs_mapping."""
        mock_wikidata_pg.fetchall = AsyncMock(
            return_value=[
                {"discogs_artist_id": 12, "qid": "Q378288"},
            ]
        )
        result = await reconciler.resolve_qids_from_discogs_ids({12, 99})
        assert result[12] == "Q378288"
        assert 99 not in result

    @pytest.mark.asyncio
    async def test_sparql_fallback(self, reconciler, mock_wikidata_pg, mock_sparql):
        """Falls back to SPARQL P1953 when cache misses."""
        mock_wikidata_pg.fetchall = AsyncMock(return_value=[])
        mock_sparql.query_batched = AsyncMock(
            return_value=[
                {
                    "item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q378288"},
                    "discogsId": {"type": "literal", "value": "12"},
                }
            ]
        )
        result = await reconciler.resolve_qids_from_discogs_ids({12})
        assert result[12] == "Q378288"
        mock_sparql.query_batched.assert_called_once()


class TestNameSearch:
    @pytest.mark.asyncio
    async def test_finds_musician_by_name(self, reconciler, mock_sparql):
        """Name search returns QID for a musician match."""
        mock_sparql.query = AsyncMock(
            return_value=[
                {
                    "item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q378288"},
                    "itemLabel": {"type": "literal", "value": "Autechre"},
                }
            ]
        )
        qid = await reconciler.search_musician_by_name("Autechre")
        assert qid == "Q378288"

    @pytest.mark.asyncio
    async def test_returns_none_on_no_results(self, reconciler, mock_sparql):
        """Name search returns None when no musician candidates found."""
        mock_sparql.query = AsyncMock(return_value=[])
        qid = await reconciler.search_musician_by_name("ZZZNONEXISTENT")
        assert qid is None


class TestStreamingIdFetch:
    @pytest.mark.asyncio
    async def test_fetches_all_streaming_ids(self, reconciler, mock_sparql):
        """Streaming ID fetch populates Spotify, Apple Music, and Bandcamp."""
        mock_sparql.query_batched = AsyncMock(
            return_value=[
                {
                    "item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q378288"},
                    "spotifyId": {"type": "literal", "value": "2cGiltMnETMr482N3F3ILQK"},
                    "appleMusicId": {"type": "literal", "value": "1234567"},
                    "bandcampId": {"type": "literal", "value": "autechre"},
                }
            ]
        )
        result = await reconciler.fetch_streaming_ids(["Q378288"])
        assert "Q378288" in result
        ids = result["Q378288"]
        assert ids.spotify_artist_id == "2cGiltMnETMr482N3F3ILQK"
        assert ids.apple_music_artist_id == "1234567"
        assert ids.bandcamp_id == "autechre"

    @pytest.mark.asyncio
    async def test_partial_streaming_ids(self, reconciler, mock_sparql):
        """Partial results: some streaming IDs may be absent."""
        mock_sparql.query_batched = AsyncMock(
            return_value=[
                {
                    "item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q378288"},
                    "spotifyId": {"type": "literal", "value": "abc123"},
                }
            ]
        )
        result = await reconciler.fetch_streaming_ids(["Q378288"])
        ids = result["Q378288"]
        assert ids.spotify_artist_id == "abc123"
        assert ids.apple_music_artist_id is None
        assert ids.bandcamp_id is None


class TestGracefulDegradation:
    @pytest.mark.asyncio
    async def test_no_wikidata_cache_uses_sparql_only(self, reconciler_no_cache, mock_sparql):
        """When wikidata_pg is None, resolve_qids_from_discogs_ids uses SPARQL only."""
        mock_sparql.query_batched = AsyncMock(
            return_value=[
                {
                    "item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q378288"},
                    "discogsId": {"type": "literal", "value": "12"},
                }
            ]
        )
        result = await reconciler_no_cache.resolve_qids_from_discogs_ids({12})
        assert result[12] == "Q378288"
        mock_sparql.query_batched.assert_called_once()
