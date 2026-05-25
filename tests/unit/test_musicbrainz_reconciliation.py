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


class TestNameMatchDeterministicTiebreak:
    """WXYC/library-metadata-lookup#378.

    ``resolve_from_names`` must pick the same MBID every call for an
    ambiguous-name query — the order PostgreSQL returns rows in is not a
    stable contract, so the SQL must impose an explicit ORDER BY and the
    Python first-wins loop must reflect that ordering.

    Tiebreak rule (in priority order):
      1. Primary-name match (mb_artist.name) beats alias match (mb_artist_alias.name)
      2. Lower mb_artist.id wins (stable across rebuilds; reflects MB insertion order)
    """

    @pytest.mark.asyncio
    async def test_sql_has_order_by(self, reconciler, mock_mb_pg):
        """The query that the reconciler issues must be sorted server-side."""
        mock_mb_pg.fetchall = AsyncMock(return_value=[])
        await reconciler.resolve_from_names(["whatever"])

        sql = mock_mb_pg.fetchall.call_args.args[0]
        assert "ORDER BY" in sql, "ORDER BY required for deterministic MBID selection"

    @pytest.mark.asyncio
    async def test_sql_orders_primary_before_alias(self, reconciler, mock_mb_pg):
        """ORDER BY must sort primary-name matches ahead of alias matches.

        The query tags each row with a ``match_kind`` (0=primary, 1=alias) so
        the same canonical name across both arms of the UNION resolves
        deterministically. ASC on match_kind makes primary win.
        """
        mock_mb_pg.fetchall = AsyncMock(return_value=[])
        await reconciler.resolve_from_names(["whatever"])

        sql = mock_mb_pg.fetchall.call_args.args[0]
        assert "match_kind" in sql, "Need match_kind discriminator for primary-vs-alias"
        # match_kind belongs in the ORDER BY (primary first), not just in the projection.
        assert "match_kind" in sql.split("ORDER BY", 1)[1]

    @pytest.mark.parametrize(
        "rows_in_order,expected_gid",
        [
            # Single primary match: trivially deterministic.
            (
                [{"name": "autechre", "gid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}],
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            ),
            # Primary match comes first (match_kind=0); alias match (match_kind=1)
            # for the same canonical name is iterated second and skipped.
            (
                [
                    {"name": "john williams", "gid": "11111111-1111-1111-1111-111111111111"},
                    {"name": "john williams", "gid": "22222222-2222-2222-2222-222222222222"},
                ],
                "11111111-1111-1111-1111-111111111111",
            ),
            # Three primary matches, sorted by mb_artist.id ASC server-side.
            # First-wins on the lowest-id row.
            (
                [
                    {"name": "john williams", "gid": "33333333-3333-3333-3333-333333333333"},
                    {"name": "john williams", "gid": "44444444-4444-4444-4444-444444444444"},
                    {"name": "john williams", "gid": "55555555-5555-5555-5555-555555555555"},
                ],
                "33333333-3333-3333-3333-333333333333",
            ),
        ],
        ids=["single-match", "primary-beats-alias", "primary-stable-tiebreak"],
    )
    @pytest.mark.asyncio
    async def test_deterministic_pick(self, reconciler, mock_mb_pg, rows_in_order, expected_gid):
        """Given rows arrive in the SQL-imposed ORDER BY sequence, first-wins
        is deterministic and matches the documented tiebreak rule.
        """
        mock_mb_pg.fetchall = AsyncMock(return_value=rows_in_order)
        canonical = next(iter({r["name"] for r in rows_in_order}))
        # Use the canonical form (title-cased) for the input — the reconciler
        # round-trips through lower() for matching.
        input_name = canonical.title()
        result = await reconciler.resolve_from_names([input_name])
        assert result[input_name] == expected_gid
