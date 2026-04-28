"""Unit tests for Wikidata identity resolution."""

from unittest.mock import AsyncMock

import pytest

from scripts.entity_resolution.sources import SparqlSource
from scripts.entity_resolution.wikidata import (
    _SPARQL_DISCOGS_TO_QID,
    _SPARQL_QID_TO_DISCOGS_ARTIST,
    _SPARQL_QID_TO_DISCOGS_MASTER,
    _SPARQL_QID_TO_DISCOGS_RELEASE,
    _SPARQL_STREAMING_IDS,
    WikidataReconciler,
)


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

    @pytest.mark.asyncio
    async def test_sparql_fallback_passes_template_and_quoted_ids(
        self, reconciler, mock_wikidata_pg, mock_sparql
    ):
        """The SPARQL fallback must hand ``query_batched`` the *unformatted*
        template and the SPARQL-quoted Discogs IDs as items, so batching
        actually splits the work and the literal SPARQL braces survive.

        Regression: WXYC/library-metadata-lookup#182. Previously the call
        site pre-substituted ``{values}`` (defeating batching) and then passed
        ``Q``-prefixed strings as items (wrong format and never used).
        """
        mock_wikidata_pg.fetchall = AsyncMock(return_value=[])
        mock_sparql.query_batched = AsyncMock(return_value=[])

        await reconciler.resolve_qids_from_discogs_ids({12, 34})

        mock_sparql.query_batched.assert_called_once()
        template_arg, items_arg = mock_sparql.query_batched.call_args.args
        assert template_arg is _SPARQL_DISCOGS_TO_QID
        assert "{values}" in template_arg
        assert set(items_arg) == {'"12"', '"34"'}


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
    async def test_passes_wd_prefixed_qids_to_query_batched(self, reconciler, mock_sparql):
        """``fetch_streaming_ids`` must hand ``query_batched`` items already
        prefixed with ``wd:`` so the rendered SPARQL ``VALUES`` clause is
        syntactically valid (bare ``Q378288`` is a SPARQL parse error).

        Regression: WXYC/library-metadata-lookup#187.
        """
        mock_sparql.query_batched = AsyncMock(return_value=[])

        await reconciler.fetch_streaming_ids(["Q378288", "Q123"])

        mock_sparql.query_batched.assert_called_once()
        template_arg, items_arg = mock_sparql.query_batched.call_args.args
        assert template_arg is _SPARQL_STREAMING_IDS
        assert items_arg == ["wd:Q378288", "wd:Q123"]

    @pytest.mark.asyncio
    async def test_rendered_sparql_uses_wd_prefix(self, mock_wikidata_pg, monkeypatch):
        """End-to-end through real ``SparqlSource``: capture the substituted
        SPARQL and confirm the VALUES clause has ``wd:Q...``, not bare ``Q...``.

        Regression: WXYC/library-metadata-lookup#187. Without the prefix,
        Wikidata's SPARQL endpoint rejects the query at parse time.
        """
        sparql = SparqlSource(batch_size=10)
        captured: list[str] = []

        async def fake_query(query_str: str):
            captured.append(query_str)
            return []

        monkeypatch.setattr(sparql, "query", fake_query)
        reconciler = WikidataReconciler(sparql=sparql, wikidata_pg=mock_wikidata_pg)

        await reconciler.fetch_streaming_ids(["Q378288", "Q123"])

        assert len(captured) == 1
        rendered = captured[0]
        assert "wd:Q378288" in rendered
        assert "wd:Q123" in rendered
        # No bare QIDs anywhere in the VALUES clause.
        assert "{ Q378288" not in rendered
        assert " Q378288 " not in rendered

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


class TestQidToDiscogsId:
    """The inverse direction of resolve_qids_from_discogs_ids.

    Wikidata QIDs are substantially more stable than Discogs IDs (which
    Discogs admins can reorganize / merge / re-assign). When a caller has a
    QID and needs the *current* Discogs ID — for example, an integration
    test that wants to fetch the canonical Discogs entity for a known
    Wikidata-pinned artist — this is the lookup direction that survives
    Discogs reshuffling.
    """

    @pytest.mark.asyncio
    async def test_artist_kind_uses_p1953(self, reconciler_no_cache, mock_sparql):
        """kind='artist' resolves QID -> P1953 (Discogs artist ID)."""
        mock_sparql.query_batched = AsyncMock(
            return_value=[
                {
                    "item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q334652"},
                    "discogsId": {"type": "literal", "value": "388"},
                }
            ]
        )
        result = await reconciler_no_cache.resolve_discogs_ids_from_qids({"Q334652"}, kind="artist")
        assert result == {"Q334652": 388}
        # Confirm the SPARQL template that was rendered targets P1953.
        template_arg = mock_sparql.query_batched.await_args.args[0]
        assert template_arg is _SPARQL_QID_TO_DISCOGS_ARTIST
        assert "wdt:P1953" in template_arg

    @pytest.mark.asyncio
    async def test_master_kind_uses_p1954(self, reconciler_no_cache, mock_sparql):
        """kind='master' resolves QID -> P1954 (Discogs master release ID)."""
        mock_sparql.query_batched = AsyncMock(
            return_value=[
                {
                    "item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q3037397"},
                    "discogsId": {"type": "literal", "value": "30682"},
                }
            ]
        )
        result = await reconciler_no_cache.resolve_discogs_ids_from_qids(
            {"Q3037397"}, kind="master"
        )
        assert result == {"Q3037397": 30682}
        template_arg = mock_sparql.query_batched.await_args.args[0]
        assert template_arg is _SPARQL_QID_TO_DISCOGS_MASTER
        assert "wdt:P1954" in template_arg

    @pytest.mark.asyncio
    async def test_release_kind_uses_p2206(self, reconciler_no_cache, mock_sparql):
        """kind='release' resolves QID -> P2206 (Discogs release ID)."""
        mock_sparql.query_batched = AsyncMock(return_value=[])
        await reconciler_no_cache.resolve_discogs_ids_from_qids({"Q123"}, kind="release")
        template_arg = mock_sparql.query_batched.await_args.args[0]
        assert template_arg is _SPARQL_QID_TO_DISCOGS_RELEASE
        assert "wdt:P2206" in template_arg

    @pytest.mark.asyncio
    async def test_invalid_kind_raises(self, reconciler_no_cache):
        """Unknown ``kind`` raises rather than silently picking a property."""
        with pytest.raises(ValueError, match="kind"):
            await reconciler_no_cache.resolve_discogs_ids_from_qids({"Q1"}, kind="label")

    @pytest.mark.asyncio
    async def test_qids_passed_with_wd_prefix(self, reconciler_no_cache, mock_sparql):
        """SPARQL VALUES clause needs `wd:` prefix on each QID; bare `Q...` is a parse error."""
        mock_sparql.query_batched = AsyncMock(return_value=[])
        await reconciler_no_cache.resolve_discogs_ids_from_qids({"Q334652"}, kind="artist")
        items_arg = mock_sparql.query_batched.await_args.args[1]
        assert items_arg == ["wd:Q334652"]

    @pytest.mark.asyncio
    async def test_empty_qids_short_circuits(self, reconciler_no_cache, mock_sparql):
        """Empty input does no SPARQL request."""
        result = await reconciler_no_cache.resolve_discogs_ids_from_qids(set(), kind="artist")
        assert result == {}
        mock_sparql.query_batched.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_property_skipped(self, reconciler_no_cache, mock_sparql):
        """A QID that has no P1953 value (e.g. obscure artist) is omitted from the result, not None-valued."""
        mock_sparql.query_batched = AsyncMock(
            return_value=[
                {
                    "item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q1"},
                    # discogsId binding intentionally absent — Wikidata returns no row
                    # at all in this case, but mirroring the defensive parsing in
                    # resolve_qids_from_discogs_ids is the right shape.
                }
            ]
        )
        result = await reconciler_no_cache.resolve_discogs_ids_from_qids({"Q1"}, kind="artist")
        assert result == {}

    @pytest.mark.asyncio
    async def test_non_integer_discogs_id_skipped(self, reconciler_no_cache, mock_sparql):
        """Defensive: a malformed Wikidata literal that does not parse as int is skipped."""
        mock_sparql.query_batched = AsyncMock(
            return_value=[
                {
                    "item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q1"},
                    "discogsId": {"type": "literal", "value": "not-a-number"},
                }
            ]
        )
        result = await reconciler_no_cache.resolve_discogs_ids_from_qids({"Q1"}, kind="artist")
        assert result == {}

    @pytest.mark.asyncio
    async def test_rendered_sparql_uses_wd_prefix(self, mock_wikidata_pg, monkeypatch):
        """End-to-end through real ``SparqlSource``: capture the substituted SPARQL
        and confirm the VALUES clause has ``wd:Q...`` and the right ``wdt:P...``
        property for each ``kind``.

        Mirrors the same defensive shape as ``test_rendered_sparql_uses_wd_prefix``
        for ``fetch_streaming_ids`` (regression: WXYC/library-metadata-lookup#187).
        Without the prefix, Wikidata's SPARQL endpoint rejects the query at parse time.
        """
        sparql = SparqlSource(batch_size=10)
        captured: list[str] = []

        async def fake_query(query_str: str):
            captured.append(query_str)
            return []

        monkeypatch.setattr(sparql, "query", fake_query)
        reconciler = WikidataReconciler(sparql=sparql, wikidata_pg=mock_wikidata_pg)

        await reconciler.resolve_discogs_ids_from_qids({"Q334652"}, kind="artist")
        await reconciler.resolve_discogs_ids_from_qids({"Q3037397"}, kind="master")
        await reconciler.resolve_discogs_ids_from_qids({"Q1"}, kind="release")

        assert len(captured) == 3
        artist_q, master_q, release_q = captured
        assert "wd:Q334652" in artist_q and "wdt:P1953" in artist_q
        assert "wd:Q3037397" in master_q and "wdt:P1954" in master_q
        assert "wd:Q1" in release_q and "wdt:P2206" in release_q
        # No bare QIDs anywhere in the rendered VALUES clauses.
        for q in captured:
            assert "{ Q" not in q, f"bare QID leaked into SPARQL: {q!r}"
