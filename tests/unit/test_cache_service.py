"""Unit tests for discogs/cache_service.py."""

import asyncio
from dataclasses import fields
from datetime import UTC
from unittest.mock import AsyncMock

import asyncpg
import pytest

from discogs.cache_service import (
    _SEARCH_BY_TRACK_ARTIST_SQL,
    _SEARCH_BY_TRACK_SQL,
    ArtistEqualityCandidates,
    CacheUnavailableError,
    DiscogsCacheService,
)
from discogs.models import (
    ArtistCredit,
    ArtistDetails,
    ArtistRef,
    LabelCredit,
    MemberRef,
    ReleaseMetadataResponse,
    ReleaseVideo,
    TrackItem,
)
from generated.api_models import ArtistResolveCacheLeg


def make_fetch_router(**table_results):
    """Create a mock side_effect that routes by table name in query.

    With asyncio.gather, the consumption order of side_effect entries is
    non-deterministic. This routes by matching table names in the SQL query.
    More specific table names (e.g., release_track_artist) must be checked
    before less specific ones (e.g., release_track).
    """
    # Sort keys longest-first so "release_track_artist" matches before "release_track"
    sorted_tables = sorted(table_results.keys(), key=len, reverse=True)

    async def route(query, *args):
        for table_name in sorted_tables:
            if table_name in query:
                return table_results[table_name]
        return []

    return route


@pytest.fixture
def cache_service(mock_asyncpg_pool):
    return DiscogsCacheService(mock_asyncpg_pool)


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    @pytest.mark.asyncio
    async def test_healthy(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=1)
        assert await cache_service.is_available() is True

    @pytest.mark.asyncio
    async def test_exception(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(side_effect=Exception("down"))
        assert await cache_service.is_available() is False


# ---------------------------------------------------------------------------
# search_releases_by_track
# ---------------------------------------------------------------------------


class TestSearchReleasesByTrack:
    # LML#804: the trigram query now runs on an acquired connection inside a
    # transaction (SET LOCAL statement_timeout + work_mem), so assertions
    # target ``mock_asyncpg_pool._mock_conn.fetch`` — same convention as
    # ``TestArtistTrigramCandidates`` / ``TestWriteRelease``.
    @pytest.mark.asyncio
    async def test_returns_results(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "Artist",
                    "track_title": "Song",
                    "is_compilation": False,
                }
            ]
        )

        results = await cache_service.search_releases_by_track("Song", "Artist")
        assert len(results) == 1
        assert results[0].album == "Album"

    @pytest.mark.asyncio
    async def test_deduplicates(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "A",
                    "track_title": "S",
                    "is_compilation": False,
                },
                {
                    "release_id": 2,
                    "title": "Album",
                    "artist_name": "A",
                    "track_title": "S",
                    "is_compilation": False,
                },
            ]
        )

        results = await cache_service.search_releases_by_track("S")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_respects_limit(self, cache_service, mock_asyncpg_pool):
        rows = [
            {
                "release_id": i,
                "title": f"Album{i}",
                "artist_name": "A",
                "track_title": "S",
                "is_compilation": False,
            }
            for i in range(10)
        ]
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(return_value=rows)

        results = await cache_service.search_releases_by_track("S", limit=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_returns_va_compilation_via_track_artist(self, cache_service, mock_asyncpg_pool):
        """VA compilation is returned when the track-level artist matches."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 99,
                    "title": "Nao Wave- Brazilian Punk 82-88",
                    "artist_name": "Various Artists",
                    "track_title": "Ciencias Sensuais",
                    "is_compilation": True,
                }
            ]
        )

        results = await cache_service.search_releases_by_track("Ciencias Sensuais", "Azul 29")
        assert len(results) == 1
        assert results[0].album == "Nao Wave- Brazilian Punk 82-88"
        assert results[0].is_compilation is True

    @pytest.mark.asyncio
    async def test_query_includes_track_artist_join(self, cache_service, mock_asyncpg_pool):
        """SQL query joins release_track_artist for track-level artist matching."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(return_value=[])

        await cache_service.search_releases_by_track("Song", "Artist")

        sql = mock_asyncpg_pool._mock_conn.fetch.call_args[0][0]
        assert "release_track_artist" in sql
        assert "rta.artist_name" in sql

    @pytest.mark.asyncio
    async def test_query_filters_rta_to_extra_zero(self, cache_service, mock_asyncpg_pool):
        """The CTE ``rta`` credit prefilter must restrict to main-artist credits.

        Mirrors the per-track filter in ``validate_track_on_release``: the
        candidate-release ranking should not surface a release just because
        a producer or writer name on the track happens to look like the
        requested artist. ``release_track_artist`` rows with ``extra = 1``
        are extra credits (writer / producer / performer) and must not
        participate in the candidate-artist match. See #333.

        The guard is pinned on the ``rta`` EXISTS leg *inside the CTE* (before
        ``LIMIT $2``) -- the leg that governs which releases survive truncation.
        A bare ``"rta.extra = 0" in sql`` check would be satisfied by the outer
        LEFT JOIN's copy of the same clause and pass even if the CTE leg's guard
        were deleted, which is the failure the prefilter exists to prevent
        (LML#802 review).
        """
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(return_value=[])

        await cache_service.search_releases_by_track("Song", "Artist")

        sql = mock_asyncpg_pool._mock_conn.fetch.call_args[0][0]
        cte = sql[: sql.index("LIMIT $2")]
        rta_leg = "EXISTS (SELECT 1 FROM release_track_artist rta"
        assert rta_leg in cte, f"Expected the rta EXISTS leg inside the CTE; got: {cte!r}"
        assert "rta.extra = 0" in cte[cte.index(rta_leg) :], (
            "The CTE rta EXISTS leg must carry `rta.extra = 0`; the outer LEFT "
            f"JOIN copy does not protect the truncation set. Got: {cte!r}"
        )

    @pytest.mark.asyncio
    async def test_deduplicates_multiple_track_artists(self, cache_service, mock_asyncpg_pool):
        """Multiple rows from LEFT JOIN (different track artists) are deduplicated."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 99,
                    "title": "Compilation",
                    "artist_name": "Various Artists",
                    "track_title": "Track",
                    "is_compilation": True,
                },
                {
                    "release_id": 99,
                    "title": "Compilation",
                    "artist_name": "Various Artists",
                    "track_title": "Track",
                    "is_compilation": True,
                },
            ]
        )

        results = await cache_service.search_releases_by_track("Track", "Artist A")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_error_raises_cache_unavailable(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(side_effect=Exception("db error"))

        with pytest.raises(CacheUnavailableError):
            await cache_service.search_releases_by_track("S")

    @pytest.mark.asyncio
    async def test_none_artist_uses_frozen_hot_path_sql(self, cache_service, mock_asyncpg_pool):
        """The artist=None branch runs `_SEARCH_BY_TRACK_SQL` verbatim.

        This is the VA / SONG_AS_TRACK hot path (#706): its plan and
        pg_stat_statements entry must stay frozen, so the None branch must
        dispatch to the unchanged string, never the artist variant.
        """
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(return_value=[])

        await cache_service.search_releases_by_track("Song", None)

        sql = mock_asyncpg_pool._mock_conn.fetch.call_args[0][0]
        assert sql is _SEARCH_BY_TRACK_SQL

    def test_hot_path_sql_matches_snapshot(self):
        """Change-detector for the frozen artist=None SQL (LML#802 AC#2).

        Whitespace-normalized, not byte-for-byte: hoisting the string to a
        module constant de-indents it and pg_stat_statements normalizes
        whitespace away regardless. What must not drift is the *structure* --
        any semantic edit to the hot-path query trips this and forces a
        deliberate re-freeze (and a fresh EXPLAIN).
        """
        snapshot = (
            "WITH matching_tracks AS ( SELECT DISTINCT rt.release_id, rt.sequence, "
            "rt.title as track_title, "
            "similarity(lower(f_unaccent(rt.title)), lower(f_unaccent($1))) as sim "
            "FROM release_track rt WHERE lower(f_unaccent(rt.title)) % lower(f_unaccent($1)) "
            "ORDER BY sim DESC LIMIT $2 ) "
            "SELECT r.id as release_id, r.title, ra.artist_name, mt.track_title, "
            "CASE WHEN lower(ra.artist_name) LIKE '%various%' THEN true ELSE false END "
            "as is_compilation FROM matching_tracks mt JOIN release r ON r.id = mt.release_id "
            "JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0 "
            "LEFT JOIN release_track_artist rta ON rta.release_id = mt.release_id "
            "AND rta.track_sequence = mt.sequence AND rta.extra = 0 "
            "WHERE ( $3::text IS NULL "
            "OR lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($3)) "
            "OR lower(f_unaccent(rta.artist_name)) % lower(f_unaccent($3)) ) "
            "ORDER BY mt.sim DESC"
        )
        assert " ".join(_SEARCH_BY_TRACK_SQL.split()) == snapshot

    @pytest.mark.asyncio
    async def test_artist_predicate_pushed_into_cte_before_limit(
        self, cache_service, mock_asyncpg_pool
    ):
        """Bug-fix-protocol structural analog (LML#802 AC#1).

        The prune is a SQL-execution artifact -- it can't be reproduced against
        a mock pool (no SQL runs; behavioural repro lives in the `pg` suite).
        This pins the *mechanism*: with an artist supplied, the emitted SQL must
        evaluate an artist EXISTS predicate on `$3` INSIDE the CTE, before the
        `LIMIT $2` truncates -- otherwise the LIMIT can prune the artist's own
        release out again.
        """
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(return_value=[])

        await cache_service.search_releases_by_track("Song", "Artist")

        sql = mock_asyncpg_pool._mock_conn.fetch.call_args[0][0]
        assert sql is _SEARCH_BY_TRACK_ARTIST_SQL
        limit_at = sql.index("LIMIT $2")
        cte_predicate = "EXISTS (SELECT 1 FROM release_artist ra"
        assert cte_predicate in sql
        assert sql.index(cte_predicate) < limit_at, (
            "artist EXISTS leg must appear inside the CTE, before LIMIT $2"
        )
        assert "lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($3))" in sql[:limit_at]
        # Companion to the rta.extra = 0 pin: the release-level leg is also
        # precision-guarded to main-artist credits inside the CTE.
        assert "ra.extra = 0" in sql[:limit_at], (
            "the release-level EXISTS leg must carry `ra.extra = 0` before LIMIT $2"
        )

    def test_artist_variant_outer_block_identical_to_hot_path(self):
        """The two search strings must share a byte-identical outer block.

        ``_SEARCH_BY_TRACK_ARTIST_SQL`` only adds the artist prefilter inside
        the CTE; everything from the outer ``SELECT`` onward is copied verbatim
        from ``_SEARCH_BY_TRACK_SQL`` as a pure subtractive backstop (the
        display-artist / ``is_compilation`` selection). Nothing else pins them
        equal -- the snapshot test covers only the None branch -- so if the two
        outer blocks drift, the artist branch could select or classify a release
        differently from the hot path with no test noticing (LML#802 review).
        """
        anchor = "SELECT r.id as release_id"
        assert _SEARCH_BY_TRACK_SQL.count(anchor) == 1
        assert _SEARCH_BY_TRACK_ARTIST_SQL.count(anchor) == 1
        hot_outer = _SEARCH_BY_TRACK_SQL[_SEARCH_BY_TRACK_SQL.index(anchor) :]
        artist_outer = _SEARCH_BY_TRACK_ARTIST_SQL[_SEARCH_BY_TRACK_ARTIST_SQL.index(anchor) :]
        assert hot_outer == artist_outer, (
            "the artist variant's outer block drifted from the hot path; mirror "
            "any edit across both SQL strings"
        )

    @pytest.mark.asyncio
    async def test_pins_search_bounds_before_fetch(self, mock_asyncpg_pool):
        """LML#804: the trigram query runs inside a transaction that first
        SET LOCALs statement_timeout + work_mem, BEFORE the fetch. Ordering is
        the contract (SET LOCAL outside a transaction is a no-op WARNING on
        real PostgreSQL), so the event sequence is recorded explicitly — the
        same shape as ``TestArtistTrigramCandidates``' floor-pin test."""
        svc = DiscogsCacheService(
            mock_asyncpg_pool, search_statement_timeout_ms=9000, search_work_mem="128MB"
        )
        conn = mock_asyncpg_pool._mock_conn
        events: list[str] = []
        tx_ctx = conn._mock_tx_ctx
        tx_ctx.__aenter__ = AsyncMock(side_effect=lambda: events.append("tx_enter") or tx_ctx)
        tx_ctx.__aexit__ = AsyncMock(side_effect=lambda *a: events.append("tx_exit") or False)
        set_local_sql: list[str] = []

        async def record_execute(sql, *args):
            set_local_sql.append(sql)
            events.append("execute")

        async def record_fetch(*args):
            events.append("fetch")
            return []

        conn.execute = AsyncMock(side_effect=record_execute)
        conn.fetch = AsyncMock(side_effect=record_fetch)

        await svc.search_releases_by_track("Song", "Artist")

        assert events == ["tx_enter", "execute", "fetch", "tx_exit"]
        assert "SET LOCAL statement_timeout = 9000" in set_local_sql[0]
        assert "SET LOCAL work_mem = '128MB'" in set_local_sql[0]

    @pytest.mark.asyncio
    async def test_query_canceled_maps_to_cache_unavailable(self, cache_service, mock_asyncpg_pool):
        """LML#804: a statement_timeout (asyncpg.QueryCanceledError) degrades to
        CacheUnavailableError — the fallthrough seam's cache-only boundary —
        never a 500. The QueryCanceledError is preserved as ``__cause__``."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            side_effect=asyncpg.QueryCanceledError("canceling statement due to statement timeout")
        )
        with pytest.raises(CacheUnavailableError) as exc_info:
            await cache_service.search_releases_by_track("Song", "Artist")
        assert isinstance(exc_info.value.__cause__, asyncpg.QueryCanceledError)


# ---------------------------------------------------------------------------
# get_release
# ---------------------------------------------------------------------------


class TestGetRelease:
    @pytest.mark.asyncio
    async def test_not_found(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=None)
        result = await cache_service.get_release(999)
        assert result is None

    @pytest.mark.asyncio
    async def test_full_metadata(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 123,
                "title": "The Game",
                "release_year": 1980,
                "artwork_url": "https://img.com/a.jpg",
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[
                    {"position": "1", "title": "Play the Game", "duration": "3:30", "sequence": 1}
                ],
                release_artist=[
                    {"artist_id": None, "artist_name": "Queen", "extra": 0, "role": None}
                ],
                release_label=[],
                release_genre=[{"genre": "Rock"}],
                release_style=[{"style": "Arena Rock"}, {"style": "Pop Rock"}],
            )
        )

        result = await cache_service.get_release(123)
        assert result is not None
        assert result.title == "The Game"
        assert result.artist == "Queen"
        assert result.genres == ["Rock"]
        assert result.styles == ["Arena Rock", "Pop Rock"]
        assert len(result.tracklist) == 1
        assert result.cached is True

    @pytest.mark.asyncio
    async def test_with_track_artists(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "title": "Compilation",
                "release_year": 2000,
                "artwork_url": None,
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[{"track_sequence": 1, "artist_name": "Some Artist"}],
                release_track=[
                    {"position": "1", "title": "Track1", "duration": None, "sequence": 1}
                ],
                release_artist=[
                    {"artist_id": None, "artist_name": "Various Artists", "extra": 0, "role": None}
                ],
                release_label=[],
                release_genre=[],
                release_style=[],
            )
        )

        result = await cache_service.get_release(1)
        assert result.tracklist[0].artists == ["Some Artist"]

    @pytest.mark.asyncio
    async def test_query_filters_release_track_artist_to_extra_zero(
        self, cache_service, mock_asyncpg_pool
    ):
        """get_release's per-track credit query must restrict to main credits.

        LML#588: without ``AND extra = 0``, producer/writer/remixer credits
        (``extra = 1``) cross-pollinate ``TrackItem.artists`` and contaminate
        ``_scan_tracklist_for_match``'s (artist, track) validation. Mirrors
        the SQL-presence pattern at ``TestValidateTrackOnRelease``'s
        ``test_query_filters_release_track_artist_to_extra_zero`` (#333).
        """
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "title": "On Your Own Love Again",
                "release_year": 2015,
                "artwork_url": None,
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
            }
        )
        captured_queries: list[str] = []

        async def capture_fetch(query, *args):
            captured_queries.append(query)
            return []

        mock_asyncpg_pool.fetch = AsyncMock(side_effect=capture_fetch)

        await cache_service.get_release(1)

        rta_queries = [q for q in captured_queries if "release_track_artist" in q]
        assert rta_queries, (
            f"Expected a query against release_track_artist; got: {captured_queries!r}"
        )
        # Normalize whitespace before the adjacency check — get_release uses
        # a multi-line triple-quoted SQL string, so ``release_track_artist``
        # and ``WHERE`` are separated by newlines + indentation in the raw
        # text. Pin the *complete* WHERE clause adjacency (not just the
        # substring ``extra = 0``, which could co-occur in an unrelated CTE
        # branch, whitespace-different variant, or a ``-- extra = 0``
        # comment) so a future refactor that drops the filter can't slip
        # through.
        normalized = " ".join(rta_queries[0].split())
        assert "release_track_artist WHERE release_id = $1 AND extra = 0" in normalized, (
            "Expected `release_track_artist WHERE release_id = $1 AND extra = 0` "
            f"in the release_track_artist query (see #588); got: {rta_queries[0]!r}"
        )

    @pytest.mark.asyncio
    async def test_builds_position_keyed_track_writers(self, cache_service, mock_asyncpg_pool):
        """LML#699 Phase 2: get_release reads extra=1 per-track credits and keys
        the writer subset by the track's display position (not track_sequence).
        Non-writer extras (producer) and the extra=0 performer credits are
        excluded from the writer map; TrackItem.artists is untouched.
        """
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "title": "An Album",
                "release_year": 2020,
                "artwork_url": None,
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
                "master_id": None,
            }
        )
        extra0 = [{"track_sequence": 1, "artist_name": "Performer One"}]
        extra1 = [
            {"track_sequence": 1, "artist_name": "Track One Writer", "role": "Written-By"},
            {"track_sequence": 2, "artist_name": "Track Two Writer", "role": "Composed By"},
            {"track_sequence": 2, "artist_name": "A Producer", "role": "Producer"},
        ]
        tracks = [
            {"position": "A1", "title": "First", "duration": None, "sequence": 1},
            {"position": "A2", "title": "Second", "duration": None, "sequence": 2},
        ]

        async def route(query, *args):
            if "release_track_artist" in query and "extra = 1" in query:
                return extra1
            if "release_track_artist" in query:
                return extra0
            if "release_track" in query:
                return tracks
            if "release_artist" in query:
                return [{"artist_id": 1, "artist_name": "Sessa", "extra": 0, "role": None}]
            return []

        mock_asyncpg_pool.fetch = AsyncMock(side_effect=route)
        result = await cache_service.get_release(1)

        assert result.track_writers is not None
        assert [c.name for c in result.track_writers["A1"]] == ["Track One Writer"]
        # A2: producer excluded — only the writer-role credit lands in the map.
        assert [c.name for c in result.track_writers["A2"]] == ["Track Two Writer"]
        assert result.track_writers["A2"][0].role == "Composed By"
        # extra=0 performer credits stay on TrackItem.artists, untouched.
        assert result.tracklist[0].artists == ["Performer One"]

    @pytest.mark.asyncio
    async def test_ambiguous_duplicate_position_dropped_not_merged(
        self, cache_service, mock_asyncpg_pool
    ):
        """LML#699: a display position claimed by more than one track (Discogs
        ``position`` is non-unique text — multi-disc / mispressed releases repeat
        it) is AMBIGUOUS and dropped from the writer map, NOT merged. Merging
        would attribute one track's composers to a co-positioned sibling on a BMI
        royalty field; dropping makes the track-level path fall back to
        release-level instead.
        """
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "title": "An Album",
                "release_year": 2020,
                "artwork_url": None,
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
                "master_id": None,
            }
        )
        extra1 = [
            {"track_sequence": 1, "artist_name": "Disc One Writer", "role": "Written-By"},
            {"track_sequence": 2, "artist_name": "Disc Two Writer", "role": "Composed By"},
        ]
        # Two distinct tracks carry the SAME display position "A1" (e.g. disc 1
        # A1 and disc 2 A1 on a release that didn't disc-qualify its positions).
        tracks = [
            {"position": "A1", "title": "Disc One Opener", "duration": None, "sequence": 1},
            {"position": "A1", "title": "Disc Two Opener", "duration": None, "sequence": 2},
        ]

        async def route(query, *args):
            if "release_track_artist" in query and "extra = 1" in query:
                return extra1
            if "release_track_artist" in query:
                return []
            if "release_track" in query:
                return tracks
            if "release_artist" in query:
                return [{"artist_id": 1, "artist_name": "Sessa", "extra": 0, "role": None}]
            return []

        mock_asyncpg_pool.fetch = AsyncMock(side_effect=route)
        result = await cache_service.get_release(1)

        # "A1" is ambiguous → dropped entirely (no merge, no bleed). It is the
        # only writer position, so the map collapses to None.
        assert result.track_writers is None

    @pytest.mark.asyncio
    async def test_ambiguous_position_dropped_unique_position_survives(
        self, cache_service, mock_asyncpg_pool
    ):
        """The drop is per-position, not all-or-nothing: a unique writer position
        survives alongside a dropped ambiguous one. And ambiguity is counted over
        ALL tracks — the colliding "A1" is shared by a writer-bearing track and a
        writer-LESS sibling, so a writer sharing a bare position with a
        credit-less track is still correctly dropped (not just collisions between
        two writer-bearing tracks).
        """
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "title": "An Album",
                "release_year": 2020,
                "artwork_url": None,
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
                "master_id": None,
            }
        )
        extra1 = [
            {"track_sequence": 1, "artist_name": "Ambiguous A1 Writer", "role": "Written-By"},
            {"track_sequence": 2, "artist_name": "Unique A2 Writer", "role": "Composed By"},
        ]
        # seq1 + seq3 both carry "A1" (seq3 has NO extra=1 writer); seq2 carries
        # the unique "A2".
        tracks = [
            {"position": "A1", "title": "First", "duration": None, "sequence": 1},
            {"position": "A2", "title": "Second", "duration": None, "sequence": 2},
            {"position": "A1", "title": "Third", "duration": None, "sequence": 3},
        ]

        async def route(query, *args):
            if "release_track_artist" in query and "extra = 1" in query:
                return extra1
            if "release_track_artist" in query:
                return []
            if "release_track" in query:
                return tracks
            if "release_artist" in query:
                return [{"artist_id": 1, "artist_name": "Sessa", "extra": 0, "role": None}]
            return []

        mock_asyncpg_pool.fetch = AsyncMock(side_effect=route)
        result = await cache_service.get_release(1)

        # "A1" ambiguous (two tracks) → dropped; "A2" unique → survives.
        assert result.track_writers is not None
        assert set(result.track_writers) == {"A2"}
        assert [c.name for c in result.track_writers["A2"]] == ["Unique A2 Writer"]

    def test_track_writers_excluded_from_json_schema_and_wire(self):
        """track_writers is internal (Field exclude=True + SkipJsonSchema): off
        the wire AND out of the OpenAPI schema, so the GET /release/{id} contract
        never advertises a property absent from every response body.
        """
        schema = ReleaseMetadataResponse.model_json_schema()
        assert "track_writers" not in schema["properties"]
        dumped = ReleaseMetadataResponse(
            release_id=1,
            title="An Album",
            artist="Sessa",
            release_url="https://discogs.com/release/1",
            track_writers={"A1": [ArtistCredit(name="A Writer", role="Written-By")]},
        ).model_dump()
        assert "track_writers" not in dumped

    @pytest.mark.asyncio
    async def test_query_reads_release_track_artist_extra_one(
        self, cache_service, mock_asyncpg_pool
    ):
        """get_release issues an extra=1 read against release_track_artist
        (alongside the extra=0 performer read) to source per-track writers.
        """
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "title": "An Album",
                "release_year": 2020,
                "artwork_url": None,
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
                "master_id": None,
            }
        )
        captured_queries: list[str] = []

        async def capture_fetch(query, *args):
            captured_queries.append(query)
            return []

        mock_asyncpg_pool.fetch = AsyncMock(side_effect=capture_fetch)

        await cache_service.get_release(1)

        rta_queries = [" ".join(q.split()) for q in captured_queries if "release_track_artist" in q]
        assert any(
            "release_track_artist WHERE release_id = $1 AND extra = 1" in q for q in rta_queries
        ), f"Expected an extra=1 release_track_artist read (LML#699); got: {rta_queries!r}"

    @pytest.mark.asyncio
    async def test_track_writers_none_when_no_extra_credits(self, cache_service, mock_asyncpg_pool):
        """A release with no extra=1 credits carries track_writers=None, not an
        empty dict — the writer-credit path treats both the same, but None keeps
        the field tidy for releases that have no per-track writers.
        """
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "title": "An Album",
                "release_year": 2020,
                "artwork_url": None,
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
                "master_id": None,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[
                    {"position": "A1", "title": "First", "duration": None, "sequence": 1}
                ],
                release_artist=[{"artist_id": 1, "artist_name": "Sessa", "extra": 0, "role": None}],
                release_label=[],
                release_genre=[],
                release_style=[],
            )
        )
        result = await cache_service.get_release(1)
        assert result.track_writers is None

    @pytest.mark.asyncio
    async def test_reads_videos(self, cache_service, mock_asyncpg_pool):
        """get_release returns videos fetched from release_video table."""
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "title": "Emperor Tomato Ketchup",
                "release_year": 1996,
                "artwork_url": None,
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[],
                release_artist=[
                    {"artist_id": None, "artist_name": "Stereolab", "extra": 0, "role": None}
                ],
                release_label=[],
                release_genre=[],
                release_style=[],
                release_video=[
                    {
                        "sequence": 1,
                        "src": "https://www.youtube.com/watch?v=abc",
                        "title": "Metronomic Underground",
                        "duration": 456,
                        "embed": True,
                    },
                    {
                        "sequence": 2,
                        "src": "https://www.youtube.com/watch?v=def",
                        "title": "French Disko",
                        "duration": 204,
                        "embed": False,
                    },
                ],
            )
        )

        result = await cache_service.get_release(1)
        assert result is not None
        assert len(result.videos) == 2
        assert result.videos[0].src == "https://www.youtube.com/watch?v=abc"
        assert result.videos[0].title == "Metronomic Underground"
        assert result.videos[0].duration == 456
        assert result.videos[0].embed is True
        assert result.videos[1].embed is False

    @pytest.mark.asyncio
    async def test_reads_videos_empty_when_none_exist(self, cache_service, mock_asyncpg_pool):
        """get_release returns empty videos list when release has no videos."""
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "title": "Aluminum Tunes",
                "release_year": 1998,
                "artwork_url": None,
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[],
                release_artist=[
                    {"artist_id": None, "artist_name": "Stereolab", "extra": 0, "role": None}
                ],
                release_label=[],
                release_genre=[],
                release_style=[],
                release_video=[],
            )
        )

        result = await cache_service.get_release(1)
        assert result is not None
        assert result.videos == []

    @pytest.mark.asyncio
    async def test_videos_gracefully_absent(self, cache_service, mock_asyncpg_pool):
        """get_release returns empty videos list when release_video table does not exist."""
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "title": "Emperor Tomato Ketchup",
                "release_year": 1996,
                "artwork_url": None,
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
            }
        )

        def raise_on_video(query, *args):
            if "release_video" in query:
                raise Exception("relation does not exist")
            return []

        mock_asyncpg_pool.fetch = AsyncMock(side_effect=raise_on_video)

        result = await cache_service.get_release(1)
        assert result is not None
        assert result.videos == []

    @pytest.mark.asyncio
    async def test_error_raises(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(side_effect=Exception("db error"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.get_release(1)


# ---------------------------------------------------------------------------
# write_release
# ---------------------------------------------------------------------------


class TestWriteRelease:
    @pytest.mark.asyncio
    async def test_writes_release(self, cache_service, mock_asyncpg_pool):
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Album",
            artist="Artist",
            year=2020,
            artwork_url="https://img.com/a.jpg",
            tracklist=[TrackItem(position="1", title="Track1", artists=["ArtistA"])],
            release_url="https://discogs.com/release/1",
        )

        await cache_service.write_release(release)
        conn = mock_asyncpg_pool._mock_conn
        assert conn.execute.call_count >= 3  # insert release, artist, delete tracks, cache_metadata
        assert conn.executemany.call_count >= 1  # insert tracks

    @pytest.mark.asyncio
    async def test_release_upsert_stamps_artwork_checked_at_now(
        self, cache_service, mock_asyncpg_pool
    ):
        """The release-row UPSERT must stamp ``artwork_checked_at = now()`` in
        the SQL itself. ``write_release`` is only invoked from the live
        Discogs-API path (via the fallthrough seam), so every call by
        definition means "we just asked Discogs about this row". The
        downstream ``is_pg_hit`` predicate reads this column to skip
        re-fetching genuinely-imageless releases; if the stamp regresses,
        LML burns Discogs rate limit on every lookup of an imageless tail
        row. Regression pin for WXYC/library-metadata-lookup#423 / backed by
        WXYC/discogs-etl#239.
        """
        release = ReleaseMetadataResponse(
            release_id=33696616,
            title="White Label Promo",
            artist="Stereolab",
            artwork_url=None,
            artwork_checked_at=None,  # API-derived; the stamp is SQL-side.
            release_url="https://discogs.com/release/33696616",
        )

        await cache_service.write_release(release)

        conn = mock_asyncpg_pool._mock_conn
        # The first execute is the release upsert.
        release_sql = conn.execute.call_args_list[0][0][0]
        assert "artwork_checked_at" in release_sql, (
            "release upsert must reference artwork_checked_at "
            "(predicate consumer in service.py:get_release)"
        )
        assert "now()" in release_sql, (
            "release upsert must stamp now() on artwork_checked_at — see WXYC#423"
        )
        # Pinned together: the EXCLUDED.artwork_checked_at line must also
        # appear so the ON CONFLICT path stamps the same now() value, not
        # leave the column at its prior value on re-upsert.
        assert "artwork_checked_at = EXCLUDED.artwork_checked_at" in release_sql, (
            "ON CONFLICT must update artwork_checked_at from EXCLUDED so the "
            "re-upsert path refreshes the stamp; without this, re-asking "
            "Discogs leaves artwork_checked_at at its prior value."
        )

    @pytest.mark.asyncio
    async def test_release_upsert_persists_master_id(self, cache_service, mock_asyncpg_pool):
        """LML#688: the release upsert must persist master_id so a cold API
        fetch warms the field into PG (matching the value discogs-etl seeds),
        and the ON CONFLICT path refreshes it from EXCLUDED on re-upsert."""
        release = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield",
            artist="Autechre",
            year=2001,
            master_id=12345,
            release_url="https://discogs.com/release/28138",
        )

        await cache_service.write_release(release)

        conn = mock_asyncpg_pool._mock_conn
        release_sql = conn.execute.call_args_list[0][0][0]
        assert "master_id" in release_sql, (
            "release upsert must persist master_id (LML#688 cache-warm path)"
        )
        assert "master_id = EXCLUDED.master_id" in release_sql, (
            "ON CONFLICT must refresh master_id from EXCLUDED on re-upsert"
        )
        release_params = conn.execute.call_args_list[0][0][1:]
        assert 12345 in release_params, (
            f"master_id value must be bound into the upsert params; got: {release_params!r}"
        )

    @pytest.mark.asyncio
    async def test_error_raises(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=Exception("fail"))
        release = ReleaseMetadataResponse(
            release_id=1,
            title="A",
            artist="B",
            release_url="https://discogs.com/release/1",
        )
        with pytest.raises(CacheUnavailableError):
            await cache_service.write_release(release)

    @pytest.mark.asyncio
    async def test_writes_enriched_release(self, cache_service, mock_asyncpg_pool):
        """write_release persists all artists, extra artists, labels, and released."""
        release = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield",
            artist="Autechre",
            year=2001,
            artwork_url="https://img.com/confield.jpg",
            release_url="https://discogs.com/release/28138",
            genres=["Electronic"],
            styles=["IDM", "Abstract"],
            artists=[
                ArtistCredit(artist_id=77, name="Autechre"),
            ],
            extra_artists=[
                ArtistCredit(artist_id=200, name="Rob Brown", role="Producer"),
                ArtistCredit(artist_id=201, name="Sean Booth", role="Producer"),
            ],
            labels=[
                LabelCredit(label_id=233, name="Warp Records", catno="WAP 159 CD"),
            ],
            released="2001-04-30",
            tracklist=[TrackItem(position="1", title="VI Scose Poise")],
        )

        await cache_service.write_release(release)
        conn = mock_asyncpg_pool._mock_conn

        # Check the release row includes 'released'
        release_call = conn.execute.call_args_list[0]
        release_sql = release_call[0][0]
        assert "released" in release_sql

        # Check release_artist rows were deleted then re-inserted (not just a single ON CONFLICT)
        all_sql = [call[0][0] for call in conn.execute.call_args_list]
        assert any("DELETE FROM release_artist" in sql for sql in all_sql)

        # Check release_label rows were written
        assert any("release_label" in sql for sql in all_sql)

        # Check genre/style rows were written
        assert any("DELETE FROM release_genre" in sql for sql in all_sql)
        assert any("DELETE FROM release_style" in sql for sql in all_sql)
        all_executemany_sql = [call[0][0] for call in conn.executemany.call_args_list]
        assert any("release_genre" in sql for sql in all_executemany_sql)
        assert any("release_style" in sql for sql in all_executemany_sql)

    @pytest.mark.asyncio
    async def test_writes_multiple_artists_as_rows(self, cache_service, mock_asyncpg_pool):
        """write_release writes ALL artists (main + extra) as separate rows."""
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Duke Ellington & John Coltrane",
            artist="Duke Ellington",
            release_url="https://discogs.com/release/1",
            artists=[
                ArtistCredit(artist_id=100, name="Duke Ellington", join=" & "),
                ArtistCredit(artist_id=101, name="John Coltrane"),
            ],
            extra_artists=[
                ArtistCredit(artist_id=300, name="Engineer Bob", role="Engineer"),
            ],
        )

        await cache_service.write_release(release)
        conn = mock_asyncpg_pool._mock_conn

        # Should use executemany for artist inserts (3 total: 2 main + 1 extra)
        executemany_calls = conn.executemany.call_args_list
        artist_inserts = [c for c in executemany_calls if "release_artist" in c[0][0]]
        assert len(artist_inserts) >= 1
        # The data should contain all 3 artists
        artist_data = artist_inserts[0][0][1]
        assert len(artist_data) == 3

    @pytest.mark.asyncio
    async def test_writes_videos(self, cache_service, mock_asyncpg_pool):
        """write_release persists videos to release_video table."""
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Emperor Tomato Ketchup",
            artist="Stereolab",
            release_url="https://discogs.com/release/1",
            videos=[
                ReleaseVideo(
                    src="https://www.youtube.com/watch?v=abc",
                    title="Metronomic Underground",
                    duration=456,
                ),
                ReleaseVideo(
                    src="https://www.youtube.com/watch?v=def",
                    title="French Disko",
                    duration=204,
                    embed=False,
                ),
            ],
        )

        await cache_service.write_release(release)
        conn = mock_asyncpg_pool._mock_conn

        all_sql = [call[0][0] for call in conn.execute.call_args_list]
        assert any("DELETE FROM release_video" in sql for sql in all_sql)

        all_executemany_sql = [call[0][0] for call in conn.executemany.call_args_list]
        assert any("release_video" in sql for sql in all_executemany_sql)

        video_insert = next(
            c for c in conn.executemany.call_args_list if "release_video" in c[0][0]
        )
        rows = video_insert[0][1]
        assert len(rows) == 2
        assert rows[0][1] == 1  # sequence 1
        assert rows[0][2] == "https://www.youtube.com/watch?v=abc"
        assert rows[0][3] == "Metronomic Underground"
        assert rows[0][4] == 456
        assert rows[0][5] is True
        assert rows[1][1] == 2  # sequence 2
        assert rows[1][5] is False

    @pytest.mark.asyncio
    async def test_write_release_without_videos_skips_insert(
        self, cache_service, mock_asyncpg_pool
    ):
        """write_release with no videos deletes old rows but skips executemany INSERT."""
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Aluminum Tunes",
            artist="Stereolab",
            release_url="https://discogs.com/release/1",
        )

        await cache_service.write_release(release)
        conn = mock_asyncpg_pool._mock_conn

        # DELETE FROM release_video should still be called (to clear stale data)
        all_sql = [call[0][0] for call in conn.execute.call_args_list]
        assert any("DELETE FROM release_video" in sql for sql in all_sql)

        # No executemany INSERT for release_video (no videos to write)
        all_executemany_sql = [call[0][0] for call in conn.executemany.call_args_list]
        assert not any("release_video" in sql for sql in all_executemany_sql)

    @pytest.mark.asyncio
    async def test_videos_table_absent_is_nonfatal(self, cache_service, mock_asyncpg_pool):
        """write_release does not raise if release_video table does not exist."""
        conn = mock_asyncpg_pool._mock_conn

        def raise_on_video(sql, *args):
            if "release_video" in sql:
                raise Exception("relation does not exist")

        conn.execute = AsyncMock(side_effect=raise_on_video)

        release = ReleaseMetadataResponse(
            release_id=1,
            title="Emperor Tomato Ketchup",
            artist="Stereolab",
            release_url="https://discogs.com/release/1",
            videos=[
                ReleaseVideo(src="https://www.youtube.com/watch?v=abc"),
            ],
        )
        # Must not raise
        await cache_service.write_release(release)

    @pytest.mark.asyncio
    async def test_write_release_wraps_in_transaction(self, cache_service, mock_asyncpg_pool):
        """write_release runs the full DELETE+INSERT cascade inside conn.transaction().

        Without the wrapper, asyncpg autocommits each statement; a mid-cascade
        cancellation leaves an artworked release with empty release_artist /
        empty tracklist / etc. See WXYC/library-metadata-lookup#375.
        """
        release = ReleaseMetadataResponse(
            release_id=1,
            title="DOGA",
            artist="Juana Molina",
            release_url="https://discogs.com/release/1",
            tracklist=[TrackItem(position="1", title="la paradoja")],
        )

        await cache_service.write_release(release)

        conn = mock_asyncpg_pool._mock_conn
        # Outer transaction + savepoints for optional tables (release_genre /
        # _style / _video) means transaction() is called more than once; we
        # only need to verify the wrap exists.
        conn.transaction.assert_called()
        conn._mock_tx_ctx.__aenter__.assert_awaited()

    @pytest.mark.asyncio
    async def test_write_release_rolls_back_on_cancellation(self, cache_service, mock_asyncpg_pool):
        """A cancellation mid-cascade propagates through the transaction's __aexit__
        (which translates to ROLLBACK on a real connection) and surfaces to the caller.
        """
        conn = mock_asyncpg_pool._mock_conn

        # cache_metadata UPSERT is the last write — fail before it to simulate
        # disconnect mid-cascade. The transaction wrapper must propagate.
        async def raise_on_cache_metadata(sql, *args):
            if "cache_metadata" in sql:
                raise asyncio.CancelledError()

        conn.execute = AsyncMock(side_effect=raise_on_cache_metadata)

        release = ReleaseMetadataResponse(
            release_id=1,
            title="DOGA",
            artist="Juana Molina",
            release_url="https://discogs.com/release/1",
        )

        with pytest.raises((asyncio.CancelledError, CacheUnavailableError)):
            await cache_service.write_release(release)

        # The transaction context manager must have been entered and exited.
        # On real asyncpg, __aexit__ with a non-None exc triggers ROLLBACK.
        conn.transaction.assert_called()
        conn._mock_tx_ctx.__aexit__.assert_awaited()


# ---------------------------------------------------------------------------
# get_release (enriched)
# ---------------------------------------------------------------------------


class TestGetReleaseEnriched:
    @pytest.mark.asyncio
    async def test_reads_enriched_metadata(self, cache_service, mock_asyncpg_pool):
        """get_release returns enriched artist, label, and released data."""
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 28138,
                "title": "Confield",
                "release_year": 2001,
                "artwork_url": "https://img.com/a.jpg",
                "released": "2001-04-30",
                "artwork_checked_at": None,
                "not_found": False,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[
                    {"position": "1", "title": "VI Scose Poise", "duration": "5:30", "sequence": 1}
                ],
                release_artist=[
                    {"artist_id": 77, "artist_name": "Autechre", "extra": 0, "role": None},
                    {"artist_id": 200, "artist_name": "Rob Brown", "extra": 1, "role": "Producer"},
                ],
                release_label=[
                    {"label_id": 233, "label_name": "Warp Records", "catno": "WAP 159 CD"},
                ],
            )
        )

        result = await cache_service.get_release(28138)
        assert result is not None
        assert result.released == "2001-04-30"
        assert len(result.artists) == 1
        assert result.artists[0].artist_id == 77
        assert result.artists[0].name == "Autechre"
        assert len(result.extra_artists) == 1
        assert result.extra_artists[0].name == "Rob Brown"
        assert result.extra_artists[0].role == "Producer"
        assert len(result.labels) == 1
        assert result.labels[0].label_id == 233
        assert result.labels[0].name == "Warp Records"
        assert result.labels[0].catno == "WAP 159 CD"
        # Backward compat scalars
        assert result.artist == "Autechre"
        assert result.label == "Warp Records"

    @pytest.mark.asyncio
    async def test_reads_master_id(self, cache_service, mock_asyncpg_pool):
        """LML#688: a master-bearing cached release surfaces its master_id.

        ``get_release`` is read-through (in-memory → PG → API). WXYC library
        releases are pre-warmed into discogs-cache PG by discogs-etl, so the PG
        tier — not the API path — serves essentially every library lookup.
        ``master_id`` must be plumbed through this path or the catalog-popularity
        caller (BS#1486) sees ``None`` for exactly the releases it cares about.
        """
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 28138,
                "title": "Confield",
                "release_year": 2001,
                "artwork_url": None,
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
                "master_id": 9999,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_artist=[
                    {"artist_id": 77, "artist_name": "Autechre", "extra": 0, "role": None}
                ],
            )
        )

        result = await cache_service.get_release(28138)
        assert result is not None
        assert result.master_id == 9999

    @pytest.mark.asyncio
    async def test_master_less_release_surfaces_none(self, cache_service, mock_asyncpg_pool):
        """A release with no Discogs master surfaces master_id=None, not an error."""
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "title": "Self-Released EP",
                "release_year": 2019,
                "artwork_url": None,
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
                "master_id": None,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_artist=[
                    {"artist_id": 5, "artist_name": "Some Artist", "extra": 0, "role": None}
                ],
            )
        )

        result = await cache_service.get_release(1)
        assert result is not None
        assert result.master_id is None

    @pytest.mark.asyncio
    async def test_get_release_select_includes_master_id(self, cache_service, mock_asyncpg_pool):
        """Pin master_id in the parent release SELECT so a future refactor that
        drops the column can't silently regress the cache-warm path (LML#688)."""
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "title": "X",
                "release_year": 2000,
                "artwork_url": None,
                "released": None,
                "artwork_checked_at": None,
                "not_found": False,
                "master_id": 42,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=make_fetch_router())

        await cache_service.get_release(1)

        select_sql = mock_asyncpg_pool.fetchrow.call_args.args[0]
        assert "master_id" in select_sql, (
            f"the parent release SELECT must fetch master_id (LML#688); got: {select_sql!r}"
        )


# ---------------------------------------------------------------------------
# search_releases
# ---------------------------------------------------------------------------


class TestSearchReleases:
    # LML#804: the trigram query now runs on an acquired connection inside a
    # transaction (SET LOCAL statement_timeout + work_mem), so assertions
    # target ``mock_asyncpg_pool._mock_conn.fetch``.
    @pytest.mark.asyncio
    async def test_no_params_returns_empty(self, cache_service):
        result = await cache_service.search_releases()
        assert result == []

    @pytest.mark.asyncio
    async def test_artist_and_album(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "Artist",
                    "artist_credits": ["Artist"],
                    "artwork_url": None,
                    "score": 0.8,
                }
            ]
        )
        result = await cache_service.search_releases(artist="Artist", album="Album")
        assert len(result) == 1
        assert result[0]["artist_credits"] == ["Artist"]

    @pytest.mark.asyncio
    async def test_aggregated_credit_passes_through(self, cache_service, mock_asyncpg_pool):
        """LML#784: the row carries the joined credit plus the per-credit list."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 36830641,
                    "title": "Cup Of Loneliness / Choices",
                    "artist_name": "Fust, Merce Lemon",
                    "artist_credits": ["Fust", "Merce Lemon"],
                    "artwork_url": None,
                    "score": 0.9,
                }
            ]
        )
        result = await cache_service.search_releases(
            artist="Merce Lemon & Fust", album="Cup of Loneliness / Choices"
        )
        assert result[0]["artist_name"] == "Fust, Merce Lemon"
        assert result[0]["artist_credits"] == ["Fust", "Merce Lemon"]

    @pytest.mark.asyncio
    async def test_artist_only(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "Artist",
                    "artist_credits": ["Artist"],
                    "artwork_url": None,
                    "score": 0.8,
                }
            ]
        )
        result = await cache_service.search_releases(artist="Artist")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_album_only(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "Artist",
                    "artist_credits": ["Artist"],
                    "artwork_url": None,
                    "score": 0.8,
                }
            ]
        )
        result = await cache_service.search_releases(album="Album")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_deduplicates(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "A1",
                    "artist_credits": ["A1"],
                    "artwork_url": None,
                    "score": 0.8,
                },
                {
                    "release_id": 2,
                    "title": "Album",
                    "artist_name": "A2",
                    "artist_credits": ["A2"],
                    "artwork_url": None,
                    "score": 0.7,
                },
            ]
        )
        result = await cache_service.search_releases(artist="A1")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_artist_and_album_query_uses_and_logic(self, cache_service, mock_asyncpg_pool):
        """When both artist and album are provided, the SQL must require BOTH to match (AND)."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(return_value=[])
        await cache_service.search_releases(artist="High Rise", album="Disallow")

        query_sql = mock_asyncpg_pool._mock_conn.fetch.call_args[0][0]
        # Normalize whitespace for reliable matching
        normalized = " ".join(query_sql.split())
        assert "AND lower(f_unaccent(ra.artist_name))" in normalized, (
            f"Expected AND between title and artist_name conditions, got: {normalized}"
        )
        assert "OR lower(f_unaccent(ra.artist_name))" not in normalized, (
            f"Expected no OR between title and artist_name conditions, got: {normalized}"
        )

    @pytest.mark.asyncio
    async def test_error_raises(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(side_effect=Exception("db error"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.search_releases(artist="A")

    @pytest.mark.asyncio
    async def test_pins_search_bounds_before_fetch(self, mock_asyncpg_pool):
        """LML#804: the trigram query runs inside a transaction that first
        SET LOCALs statement_timeout + work_mem, BEFORE the fetch. The event
        sequence is recorded explicitly (SET LOCAL outside a transaction is a
        no-op WARNING on real PostgreSQL, so ordering is the contract)."""
        svc = DiscogsCacheService(
            mock_asyncpg_pool, search_statement_timeout_ms=9000, search_work_mem="128MB"
        )
        conn = mock_asyncpg_pool._mock_conn
        events: list[str] = []
        tx_ctx = conn._mock_tx_ctx
        tx_ctx.__aenter__ = AsyncMock(side_effect=lambda: events.append("tx_enter") or tx_ctx)
        tx_ctx.__aexit__ = AsyncMock(side_effect=lambda *a: events.append("tx_exit") or False)
        set_local_sql: list[str] = []

        async def record_execute(sql, *args):
            set_local_sql.append(sql)
            events.append("execute")

        async def record_fetch(*args):
            events.append("fetch")
            return []

        conn.execute = AsyncMock(side_effect=record_execute)
        conn.fetch = AsyncMock(side_effect=record_fetch)

        await svc.search_releases(artist="Stereolab")

        assert events == ["tx_enter", "execute", "fetch", "tx_exit"]
        assert "SET LOCAL statement_timeout = 9000" in set_local_sql[0]
        assert "SET LOCAL work_mem = '128MB'" in set_local_sql[0]

    @pytest.mark.asyncio
    async def test_query_canceled_maps_to_cache_unavailable(self, cache_service, mock_asyncpg_pool):
        """LML#804: a statement_timeout (asyncpg.QueryCanceledError) degrades to
        CacheUnavailableError — the fallthrough seam's cache-only boundary —
        never a 500. The QueryCanceledError is preserved as ``__cause__``."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            side_effect=asyncpg.QueryCanceledError("canceling statement due to statement timeout")
        )
        with pytest.raises(CacheUnavailableError) as exc_info:
            await cache_service.search_releases(artist="Stereolab")
        assert isinstance(exc_info.value.__cause__, asyncpg.QueryCanceledError)


class TestSearchArmBoundsBuilder:
    """LML#804: the settings-wired SET LOCAL preamble builder."""

    def test_default_bounds_from_settings(self):
        """A service built with no override wires the documented defaults
        (statement_timeout 10s, work_mem 128MB) from Settings."""
        from unittest.mock import MagicMock

        svc = DiscogsCacheService(MagicMock())
        assert "SET LOCAL statement_timeout = 10000" in svc._search_bounds_sql
        assert "SET LOCAL work_mem = '128MB'" in svc._search_bounds_sql

    def test_settings_expose_documented_defaults(self):
        from config.settings import Settings

        s = Settings()
        assert s.discogs_search_statement_timeout_ms == 10000
        assert s.discogs_search_work_mem == "128MB"

    def test_settings_reject_invalid_work_mem_at_load(self):
        """A bad ``DISCOGS_SEARCH_WORK_MEM`` fails at Settings load (loud, at
        boot) — not lazily at first ``DiscogsCacheService`` construction. Keeps
        the field symmetric with ``discogs_search_statement_timeout_ms``' ``ge=1``.
        """
        from pydantic import ValidationError

        from config.settings import Settings

        for bad in ("garbage", "128mb", "256 MB", "128MB\n", ""):
            with pytest.raises(ValidationError, match="Postgres memory value"):
                Settings(discogs_search_work_mem=bad)
        # A valid non-default value still loads.
        assert Settings(discogs_search_work_mem="256MB").discogs_search_work_mem == "256MB"

    def test_rejects_non_positive_timeout(self):
        from discogs.cache_service import _build_search_bounds_sql

        with pytest.raises(ValueError, match=">= 1"):
            _build_search_bounds_sql(0, "128MB")

    def test_rejects_injecting_work_mem(self):
        """work_mem is interpolated (SET LOCAL takes no bind params), so a value
        that isn't a bare Postgres memory literal is rejected up front.

        The ``"128MB\\n..."`` cases pin the ``\\Z`` (not ``$``) anchor: Python's
        ``$`` matches just before a trailing newline, so ``"128MB\\n"`` would slip
        past a ``$``-anchored regex and reach Postgres as an invalid value."""
        from discogs.cache_service import _build_search_bounds_sql

        for bad in (
            "128 MB",
            "128MB'; DROP TABLE release; --",
            "lots",
            "",
            "128MB\n",
            "128MB\nDROP TABLE release",
        ):
            with pytest.raises(ValueError, match="Postgres memory value"):
                _build_search_bounds_sql(10000, bad)

    def test_accepts_valid_work_mem_units(self):
        from discogs.cache_service import _build_search_bounds_sql

        for good in ("64MB", "128MB", "1GB", "262144kB", "4096"):
            sql = _build_search_bounds_sql(10000, good)
            assert f"work_mem = '{good}'" in sql


# ---------------------------------------------------------------------------
# validate_track_on_release
# ---------------------------------------------------------------------------


class TestValidateTrackOnRelease:
    @pytest.mark.asyncio
    async def test_not_cached_returns_none(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=False)
        result = await cache_service.validate_track_on_release(999, "Song", "Artist")
        assert result is None

    @pytest.mark.asyncio
    async def test_found(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[{"sequence": 1, "title": "Song"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "Artist"})
        result = await cache_service.validate_track_on_release(1, "Song", "Artist")
        assert result is True

    @pytest.mark.asyncio
    async def test_not_found(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[{"sequence": 1, "title": "Other Song"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "Artist"})
        result = await cache_service.validate_track_on_release(1, "Missing Song", "Artist")
        assert result is False

    @pytest.mark.asyncio
    async def test_diacritics_in_track_title(self, cache_service, mock_asyncpg_pool):
        """Track title with diacritics should match the unaccented search query."""
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[{"track_sequence": 1, "artist_name": "Azul 29"}],
                release_track=[{"sequence": 1, "title": "Ciências Sensuais"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "Various Artists"})
        result = await cache_service.validate_track_on_release(1, "Ciencias Sensuais", "Azul 29")
        assert result is True

    @pytest.mark.asyncio
    async def test_error_raises(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(side_effect=Exception("db error"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.validate_track_on_release(1, "Song", "Artist")

    @pytest.mark.asyncio
    async def test_per_track_credits_fall_back_to_release_artist(
        self, cache_service, mock_asyncpg_pool
    ):
        """Per-track credits list members/producers, not the band itself.

        Live 93 by The Orb has Towers Of Dub credited to the band members
        (Alex Paterson, Kris Weston, Thomas Fehlmann). The discogs-cache
        release_track_artist table stores those credits without a role
        distinction, so a request for "Towers Of Dub by The Orb" must
        still validate by falling back to the release-level artist.
        """
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[
                    {"track_sequence": 5, "artist_name": "Alex Paterson"},
                    {"track_sequence": 5, "artist_name": "Kris Weston"},
                    {"track_sequence": 5, "artist_name": "Thomas Fehlmann"},
                ],
                release_track=[{"sequence": 5, "title": "Towers Of Dub"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "The Orb"})
        result = await cache_service.validate_track_on_release(13938, "Towers of Dub", "The Orb")
        assert result is True

    @pytest.mark.asyncio
    async def test_per_track_credits_release_artist_fallback_still_rejects_unrelated(
        self, cache_service, mock_asyncpg_pool
    ):
        """Release-level fallback must not bleed into unrelated-artist matches.

        Requesting "Towers Of Dub by Stereolab" against the same Live 93
        cache row must return False even though we now consult the
        release-level artist after per-track credits fail.
        """
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[
                    {"track_sequence": 5, "artist_name": "Alex Paterson"},
                    {"track_sequence": 5, "artist_name": "Kris Weston"},
                    {"track_sequence": 5, "artist_name": "Thomas Fehlmann"},
                ],
                release_track=[{"sequence": 5, "title": "Towers Of Dub"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "The Orb"})
        result = await cache_service.validate_track_on_release(13938, "Towers of Dub", "Stereolab")
        assert result is False

    @pytest.mark.asyncio
    async def test_main_credit_validates_but_extra_credit_request_does_not(
        self, cache_service, mock_asyncpg_pool
    ):
        """Characterize the post-#333 filtered shape on a real example.

        Live 93 / Towers Of Dub has Discogs credits like ``The Orb`` as the
        main per-track credit (``extra = 0``) and members ``Alex Paterson``
        et al. as extra per-track credits (``extra = 1``). After the SQL
        adds ``AND extra = 0`` to the per-track read, the DB returns only
        the main credit. The mock here mirrors that post-filter state.

        Pins two behaviors at once:

        1. ``("The Orb", "Towers of Dub")`` → True via the surviving main
           credit. The existing release-level fallback isn't exercised.
        2. ``("Alex Paterson", "Towers of Dub")`` → False. The extra credit
           that previously would have validated this request has been
           filtered at the DB; the release-level fallback (``The Orb``)
           doesn't fuzz-match ``Alex Paterson``, so the request fails.

        Documents the intended post-filter contract; a future change that
        re-introduces extra credits into the per-track path or relaxes the
        release-level fuzz threshold should break this test.
        """
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                # Post-filter: only the main credit comes back.
                release_track_artist=[{"track_sequence": 5, "artist_name": "The Orb"}],
                release_track=[{"sequence": 5, "title": "Towers Of Dub"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "The Orb"})

        orb_result = await cache_service.validate_track_on_release(
            13938, "Towers of Dub", "The Orb"
        )
        assert orb_result is True

        paterson_result = await cache_service.validate_track_on_release(
            13938, "Towers of Dub", "Alex Paterson"
        )
        assert paterson_result is False

    @pytest.mark.asyncio
    async def test_track_title_singular_plural_typo_validates_via_fuzzy(
        self, cache_service, mock_asyncpg_pool
    ):
        """Singular/plural typo survives the cache-path fuzzy title fallback (LML#334).

        Mirrors the API path: ``tower of dub`` against the cached ``Towers Of Dub``
        row on Live 93. The substring gate fails on the trailing ``s``; the
        ``token_set_ratio`` fallback (~96) clears the 85 floor and the release-level
        credit (``The Orb``) validates the artist.
        """
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[
                    {"track_sequence": 5, "artist_name": "Alex Paterson"},
                    {"track_sequence": 5, "artist_name": "Kris Weston"},
                    {"track_sequence": 5, "artist_name": "Thomas Fehlmann"},
                ],
                release_track=[{"sequence": 5, "title": "Towers Of Dub"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "The Orb"})
        result = await cache_service.validate_track_on_release(13938, "tower of dub", "The Orb")
        assert result is True

    @pytest.mark.asyncio
    async def test_track_title_missing_word_validates_via_fuzzy(
        self, cache_service, mock_asyncpg_pool
    ):
        """A missing interior word survives the cache-path fuzzy title fallback.

        ``smells teen spirit`` (request dropped ``like``) vs cached
        ``Smells Like Teen Spirit`` scores 100. LML#334.
        """
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[{"sequence": 1, "title": "Smells Like Teen Spirit"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "The Artist"})
        result = await cache_service.validate_track_on_release(
            1, "smells teen spirit", "The Artist"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_track_title_fuzzy_rejects_no_token_overlap(
        self, cache_service, mock_asyncpg_pool
    ):
        """Cache-path title fuzzy fallback rejects titles with no token overlap.

        Artist trivially matches, so ``False`` isolates the title gate:
        ``towers of dub`` vs ``a perfect day`` scores ~31. LML#334.
        """
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[{"sequence": 1, "title": "A Perfect Day"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "The Artist"})
        result = await cache_service.validate_track_on_release(1, "towers of dub", "The Artist")
        assert result is False

    @pytest.mark.asyncio
    async def test_track_title_fuzzy_rejects_one_shared_token(
        self, cache_service, mock_asyncpg_pool
    ):
        """One shared significant token stays below the 85 floor on the cache path.

        ``towers of dub`` vs ``towers of london`` scores ~82 — the adversarial
        near-miss the threshold rejects. Artist trivially matches. LML#334.
        """
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[{"sequence": 1, "title": "Towers Of London"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "The Artist"})
        result = await cache_service.validate_track_on_release(1, "towers of dub", "The Artist")
        assert result is False

    @pytest.mark.asyncio
    async def test_track_title_fuzzy_rejects_partial_phrase_overlap(
        self, cache_service, mock_asyncpg_pool
    ):
        """A shared leading phrase with divergent tail stays rejected on the cache path.

        ``smells like teen spirit`` vs ``smells like the bicep`` scores ~73. LML#334.
        """
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[{"sequence": 1, "title": "Smells Like The Bicep"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "The Artist"})
        result = await cache_service.validate_track_on_release(
            1, "smells like teen spirit", "The Artist"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_query_filters_release_track_artist_to_extra_zero(
        self, cache_service, mock_asyncpg_pool
    ):
        """The per-track credit query must restrict to main-artist credits.

        After WXYC/discogs-etl#221 + WXYC/discogs-xml-converter#55 (2026-05-14),
        ``release_track_artist`` has an ``extra`` column that distinguishes
        main credits (``extra = 0``) from extra credits — writer, producer,
        performer (``extra = 1``). The per-track read must only consider
        main credits so a producer or performer name can't cross-pollinate
        a precision match; the release-level fallback added in #328 stays
        as defense-in-depth for legitimate per-track credit misses.
        """
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        captured_queries: list[str] = []

        async def capture_fetch(query, *args):
            captured_queries.append(query)
            return []

        mock_asyncpg_pool.fetch = AsyncMock(side_effect=capture_fetch)
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=None)

        await cache_service.validate_track_on_release(1, "Song", "Artist")

        rta_queries = [q for q in captured_queries if "release_track_artist" in q]
        assert rta_queries, (
            f"Expected a query against release_track_artist; got: {captured_queries!r}"
        )
        rta_query = rta_queries[0]
        # Pin the *complete* WHERE clause adjacency, not just the substring
        # ``extra = 0`` (which could co-occur in an unrelated CTE branch,
        # whitespace-different variant, or even a `-- extra = 0` comment).
        # The intent is: this specific table's WHERE includes both predicates.
        # Include the leading space before WHERE so a future delete-the-
        # trailing-space typo on the adjacent string literal in
        # cache_service.py (which would produce
        # ``release_track_artistWHERE`` — a PG parse error at runtime) also
        # fails this unit test instead of slipping through.
        assert "release_track_artist WHERE release_id = $1 AND extra = 0" in rta_query, (
            "Expected exact `release_track_artist WHERE release_id = $1 AND extra = 0` "
            f"clause in release_track_artist query (see #333); got: {rta_query!r}"
        )


# ---------------------------------------------------------------------------
# Artist detail caching
# ---------------------------------------------------------------------------


class TestGetArtistDetails:
    @pytest.mark.asyncio
    async def test_not_found(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=None)
        result = await cache_service.get_artist_details(999)
        assert result is None

    @pytest.mark.asyncio
    async def test_full_details(self, cache_service, mock_asyncpg_pool):
        from datetime import datetime

        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 77,
                "name": "Autechre",
                "profile": "Electronic duo from Rochdale.",
                "image_url": "https://i.discogs.com/autechre.jpg",
                "fetched_at": datetime(2026, 1, 1, tzinfo=UTC),
                "not_found": False,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                artist_alias=[{"alias_id": 500, "alias_name": "Gescom"}],
                artist_name_variation=[{"name": "Ae"}, {"name": "Autechre."}],
                artist_member=[{"member_id": 200, "member_name": "Rob Brown", "active": True}],
                artist_url=[{"url": "https://autechre.ws"}],
            )
        )

        result = await cache_service.get_artist_details(77)
        assert result is not None
        assert result.artist_id == 77
        assert result.name == "Autechre"
        assert result.profile == "Electronic duo from Rochdale."
        assert result.image_url == "https://i.discogs.com/autechre.jpg"
        assert len(result.aliases) == 1
        assert result.aliases[0].name == "Gescom"
        assert result.name_variations == ["Ae", "Autechre."]
        assert len(result.members) == 1
        assert result.members[0].name == "Rob Brown"
        assert result.urls == ["https://autechre.ws"]
        assert result.cached is True

    @pytest.mark.asyncio
    async def test_projects_fetched_at_for_stub_discrimination(
        self, cache_service, mock_asyncpg_pool
    ):
        """SELECT must project `fetched_at` so callers can distinguish stub
        rows (rebuild-created, never asked Discogs) from fully-fetched rows.

        See WXYC/library-metadata-lookup#502.
        """
        from datetime import datetime

        stamp = datetime(2026, 1, 1, tzinfo=UTC)
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 77,
                "name": "Stereolab",
                "profile": "Anglo-French band.",
                "image_url": None,
                "fetched_at": stamp,
                "not_found": False,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=make_fetch_router())

        result = await cache_service.get_artist_details(77)
        assert result is not None
        assert result.fetched_at == stamp

        sql = mock_asyncpg_pool.fetchrow.call_args.args[0]
        assert "fetched_at" in sql, (
            f"get_artist_details SELECT must project fetched_at; got: {sql!r}"
        )

    @pytest.mark.asyncio
    async def test_stub_row_has_null_fetched_at(self, cache_service, mock_asyncpg_pool):
        """A stub row created by the monthly rebuild has `fetched_at IS NULL`;
        the cache surfaces that as `ArtistDetails.fetched_at is None` so the
        service-layer predicate can treat it as a miss (#502).
        """
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 6998498,
                "name": "Yetsuby",
                "profile": None,
                "image_url": None,
                "fetched_at": None,
                "not_found": False,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=make_fetch_router())

        result = await cache_service.get_artist_details(6998498)
        assert result is not None
        assert result.fetched_at is None
        assert result.profile is None


class TestWriteArtistDetails:
    @pytest.mark.asyncio
    async def test_writes_artist_details(self, cache_service, mock_asyncpg_pool):
        details = ArtistDetails(
            artist_id=77,
            name="Autechre",
            profile="Electronic duo.",
            image_url="https://i.discogs.com/autechre.jpg",
            aliases=[ArtistRef(id=500, name="Gescom")],
            name_variations=["Ae"],
            members=[MemberRef(id=200, name="Rob Brown", active=True)],
            urls=["https://autechre.ws"],
        )

        await cache_service.write_artist_details(details)
        conn = mock_asyncpg_pool._mock_conn

        # Should upsert artist row
        artist_call = conn.execute.call_args_list[0]
        assert "artist" in artist_call[0][0].lower()

        # Should write child tables
        all_sql = [call[0][0] for call in conn.execute.call_args_list]
        assert any("artist_alias" in sql for sql in all_sql)
        assert any("artist_name_variation" in sql for sql in all_sql)
        assert any("artist_member" in sql for sql in all_sql)
        assert any("artist_url" in sql for sql in all_sql)

    @pytest.mark.asyncio
    async def test_write_artist_details_wraps_in_transaction(
        self, cache_service, mock_asyncpg_pool
    ):
        """write_artist_details runs UPSERT + 4×DELETE+INSERT inside conn.transaction().

        Without the wrapper, a mid-cascade cancellation leaves an artist row
        with empty aliases / variations / members / urls. See WXYC/library-metadata-lookup#375.
        """
        details = ArtistDetails(
            artist_id=77,
            name="Autechre",
            aliases=[ArtistRef(id=500, name="Gescom")],
        )

        await cache_service.write_artist_details(details)

        conn = mock_asyncpg_pool._mock_conn
        conn.transaction.assert_called()
        conn._mock_tx_ctx.__aenter__.assert_awaited()
        conn._mock_tx_ctx.__aexit__.assert_awaited()


class TestWriteArtistDetailsFetchedAtInvariant:
    """Pin the cache-hit discriminator invariant for the artist row writer.

    `DiscogsService.get_artist_details` distinguishes a stub row (created by
    the out-of-repo monthly rebuild's stub-from-`release_artist` path) from a
    fully-fetched row using `ArtistDetails.fetched_at is not None`. The
    invariant the discriminator depends on: any row written by LML has
    `fetched_at` set; only rebuild-created stubs have `fetched_at IS NULL`.

    `write_artist_details` is the only in-repo writer; both its INSERT and
    its ON CONFLICT UPDATE branches must stamp `fetched_at`. If a future
    overload or sibling writer omits it, the discriminator silently regresses
    into cache thrash (real rows treated as stubs and re-fetched on every
    call) -- a bug that wouldn't show up in functional tests. These SQL
    introspections fail the moment the column drops out of either branch.

    See WXYC/library-metadata-lookup#502 (discriminator) and #511 (this pin).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "clause_marker",
        [
            # INSERT branch -- fresh row written by LML.
            "INSERT INTO artist",
            # ON CONFLICT branch -- row was already present (typically a
            # rebuild-created stub) and LML is hydrating it.
            "ON CONFLICT",
        ],
    )
    async def test_artist_upsert_sets_fetched_at(
        self, cache_service, mock_asyncpg_pool, clause_marker
    ):
        """The single UPSERT statement must stamp `fetched_at` on BOTH branches.

        We rely on this statement being a single INSERT ... ON CONFLICT (id) DO
        UPDATE -- parameterising the marker keeps each branch's failure mode
        distinct: drop the INSERT column and the first parametrisation fails;
        drop the UPDATE-clause assignment and the second fails. Either way,
        the discriminator is broken and this test catches it.
        """
        details = ArtistDetails(artist_id=77, name="Autechre")

        await cache_service.write_artist_details(details)

        conn = mock_asyncpg_pool._mock_conn
        all_sql = [call.args[0] for call in conn.execute.call_args_list]
        artist_upsert = next(
            (sql for sql in all_sql if "INSERT INTO artist" in sql and "ON CONFLICT" in sql),
            None,
        )
        assert artist_upsert is not None, (
            "Expected a single INSERT INTO artist ... ON CONFLICT statement; "
            f"got execute calls: {all_sql!r}"
        )

        # Locate the slice of SQL that corresponds to the branch under test.
        branch_start = artist_upsert.index(clause_marker)
        if clause_marker == "INSERT INTO artist":
            branch_sql = artist_upsert[branch_start : artist_upsert.index("ON CONFLICT")]
        else:
            branch_sql = artist_upsert[branch_start:]

        assert "fetched_at" in branch_sql, (
            f"write_artist_details must stamp fetched_at on the {clause_marker} "
            f"branch of the artist UPSERT -- the cache-hit discriminator in "
            f"DiscogsService.get_artist_details depends on it (#502). "
            f"Branch SQL: {branch_sql!r}"
        )

    @pytest.mark.asyncio
    async def test_artist_upsert_stamps_fetched_at_with_now(self, cache_service, mock_asyncpg_pool):
        """`fetched_at` must be stamped server-side (`now()`), not bound as a parameter.

        If a future refactor binds `fetched_at` as a positional arg, a caller
        could pass None and silently revive the stub-row foot-gun the
        discriminator was meant to close. Forcing the value to live in SQL
        keeps it impossible to write a row with `fetched_at IS NULL` from
        this method.
        """
        details = ArtistDetails(artist_id=77, name="Autechre")

        await cache_service.write_artist_details(details)

        conn = mock_asyncpg_pool._mock_conn
        all_sql = [call.args[0] for call in conn.execute.call_args_list]
        artist_upsert = next(
            sql for sql in all_sql if "INSERT INTO artist" in sql and "ON CONFLICT" in sql
        )

        # `fetched_at = now()` (UPDATE branch) and `... now())` (INSERT branch
        # value list) -- both compact forms are acceptable. The point is that
        # `now()` appears at least twice in the statement: once per branch.
        # Whitespace-tolerant: collapse runs of whitespace before counting.
        compact = " ".join(artist_upsert.split())
        assert compact.count("now()") >= 2, (
            "fetched_at must be stamped with now() on both INSERT and ON "
            "CONFLICT UPDATE branches -- expected at least two now() calls "
            f"in the artist UPSERT, got: {artist_upsert!r}"
        )


class TestGetArtistDetailsBulkStubSemantics:
    """Pin the bulk path's stub-vs-real semantics post-#520.

    `get_artist_details_bulk` projects `fetched_at` from the artist table
    (cache_service.py:1154) and threads the value onto each returned
    `ArtistDetails`. The bulk path stays a faithful cache read -- both stub
    and hydrated rows appear in the result dict -- but the caller can now
    tell them apart using the same `fetched_at is None` discriminator the
    singular `get_artist_details` path exposes (#503).

    Locked semantics:

    * Stub row (DB: ``fetched_at IS NULL``) -> result entry with
      ``fetched_at is None``. Caller treats as a stub-shaped cache hit; the
      "is this hydrated?" judgement remains a service-layer policy.
    * Hydrated row (DB: ``fetched_at IS NOT NULL``) -> result entry with the
      row's actual timestamp.
    * Both ids are present in the returned dict. Stubs are never filtered
      out at the cache layer; that decision belongs to callers (composer
      currently ignores the discriminator; future callers don't have to).

    See WXYC/library-metadata-lookup#503 (discriminator), #520 (bulk SELECT
    projects fetched_at), #511 (this pin).
    """

    @pytest.mark.asyncio
    async def test_stub_row_surfaces_with_null_fetched_at(self, cache_service, mock_asyncpg_pool):
        """A stub row written by the rebuild path comes back with ``fetched_at=None``."""
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                **{
                    "FROM artist ": [
                        {
                            "id": 2154,
                            "name": "Stereolab",
                            "profile": None,
                            "image_url": None,
                            "fetched_at": None,
                            "not_found": False,
                        },
                    ],
                    "artist_alias": [],
                    "artist_name_variation": [],
                    "artist_member": [],
                }
            )
        )

        result = await cache_service.get_artist_details_bulk([2154])

        assert 2154 in result, "stub row must be surfaced (not filtered as a miss)"
        assert result[2154].fetched_at is None, (
            "bulk SELECT projects fetched_at; stub rows carry NULL through"
        )
        assert result[2154].cached is True, "bulk path tags every row cached=True"

    @pytest.mark.asyncio
    async def test_stub_and_hydrated_distinguishable_by_fetched_at(
        self, cache_service, mock_asyncpg_pool
    ):
        """Bulk callers CAN tell stubs from hydrated rows via ``fetched_at``.

        Post-#520, the bulk SELECT carries the column through, so the same
        ``fetched_at is None`` predicate that the singular ``is_pg_hit`` uses
        (discogs/service.py:1014) works on bulk results too. Both rows are
        still present in the returned dict -- filtering remains a
        service-layer decision.
        """
        from datetime import datetime

        hydrated_ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                **{
                    "FROM artist ": [
                        # Stub (DB: fetched_at IS NULL).
                        {
                            "id": 2154,
                            "name": "Stereolab",
                            "profile": None,
                            "image_url": None,
                            "fetched_at": None,
                            "not_found": False,
                        },
                        # Fully-fetched (DB: fetched_at IS NOT NULL).
                        {
                            "id": 305253,
                            "name": "Juana Molina",
                            "profile": "Argentinian artist...",
                            "image_url": "https://example.com/jm.jpg",
                            "fetched_at": hydrated_ts,
                            "not_found": False,
                        },
                    ],
                    "artist_alias": [],
                    "artist_name_variation": [],
                    "artist_member": [],
                }
            )
        )

        result = await cache_service.get_artist_details_bulk([2154, 305253])

        # Both surface (no cache-layer filtering)...
        assert set(result.keys()) == {2154, 305253}
        # ...but the caller can distinguish them by fetched_at.
        assert result[2154].fetched_at is None
        assert result[305253].fetched_at == hydrated_ts
        assert result[2154].cached is True
        assert result[305253].cached is True

    @pytest.mark.asyncio
    async def test_bulk_select_projects_fetched_at(self, cache_service, mock_asyncpg_pool):
        """SQL contract: the bulk artist SELECT projects ``fetched_at``.

        The bulk path's stub-vs-real discriminator (pinned above) depends on
        this column being present in the projection. If a future change drops
        it, every bulk row would silently look like a stub -- exactly the
        asymmetry #520 fixed. Catching the regression at the SQL layer is
        cheap; the seed-and-advance assertion against real PG lives in
        ``tests/integration/test_cache_service_artist_writer.py``.
        """
        mock_asyncpg_pool.fetch = AsyncMock(return_value=[])
        await cache_service.get_artist_details_bulk([2154])

        artist_table_sql = next(
            call.args[0]
            for call in mock_asyncpg_pool.fetch.await_args_list
            if "FROM artist " in call.args[0]
        )
        assert "fetched_at" in artist_table_sql, (
            "Bulk artist SELECT must project fetched_at so callers can carry "
            "the stub-vs-hydrated discriminator (#503, #520). Dropping it "
            "silently regresses every bulk row to looking like a stub."
        )


class TestSearchArtistsByName:
    @pytest.mark.asyncio
    async def test_returns_canonical_artist(self, cache_service, mock_asyncpg_pool):
        """Trigram hit returns the canonical artist row."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[
                {"id": 9001, "name": "Stereolab", "score": 0.93},
            ]
        )
        results = await cache_service.search_artists_by_name("Stereolab")
        assert results == [{"id": 9001, "name": "Stereolab", "score": 0.93}]

    @pytest.mark.asyncio
    async def test_query_unions_artist_and_variation_tables(self, cache_service, mock_asyncpg_pool):
        """The SQL must use the trigram operator and union the variation table."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(return_value=[])
        await cache_service.search_artists_by_name("Juana Molina", limit=3)
        call = mock_asyncpg_pool._mock_conn.fetch.call_args
        sql = call.args[0]
        assert "artist" in sql
        assert "artist_name_variation" in sql
        assert "f_unaccent" in sql
        assert "%" in sql  # trigram operator
        assert call.args[1] == "Juana Molina"
        assert call.args[2] == 3

    @pytest.mark.asyncio
    async def test_raises_cache_unavailable_on_pool_error(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(side_effect=RuntimeError("conn lost"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.search_artists_by_name("anything")

    @pytest.mark.asyncio
    async def test_query_canceled_maps_to_cache_unavailable(self, cache_service, mock_asyncpg_pool):
        """LML#815: this arm now runs inside the SET LOCAL statement_timeout
        bound, so a runaway trigram scan is cancelled server-side as
        ``asyncpg.QueryCanceledError`` and degrades to ``CacheUnavailableError``
        (cache-only, never a 500), preserving the cancel as ``__cause__``."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            side_effect=asyncpg.QueryCanceledError("canceling statement due to statement timeout")
        )
        with pytest.raises(CacheUnavailableError) as exc_info:
            await cache_service.search_artists_by_name("ArtistTimeoutA")
        assert isinstance(exc_info.value.__cause__, asyncpg.QueryCanceledError)

    @pytest.mark.asyncio
    async def test_cache_hit_skips_db(self, cache_service, mock_asyncpg_pool):
        """Repeated call with same args must not re-issue the underlying PG query.

        WXYC/library-metadata-lookup#359: ``search_artists_by_name`` is the
        dominant DB chokepoint (p50 = 303ms × ~1k calls/day). A per-process
        TTL cache keyed by (name, limit) collapses repeat lookups to a hash.
        """
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[{"id": 1, "name": "Stereolab", "score": 0.91}]
        )

        first = await cache_service.search_artists_by_name("Stereolab")
        second = await cache_service.search_artists_by_name("Stereolab")

        assert first == second == [{"id": 1, "name": "Stereolab", "score": 0.91}]
        assert mock_asyncpg_pool._mock_conn.fetch.await_count == 1

    @pytest.mark.asyncio
    async def test_cache_miss_different_name_hits_db(self, cache_service, mock_asyncpg_pool):
        """Different names produce different cache entries — each hits PG once."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            side_effect=[
                [{"id": 1, "name": "Stereolab", "score": 0.91}],
                [{"id": 2, "name": "Cat Power", "score": 0.88}],
            ]
        )

        await cache_service.search_artists_by_name("Stereolab")
        await cache_service.search_artists_by_name("Cat Power")

        assert mock_asyncpg_pool._mock_conn.fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_cache_miss_different_limit_hits_db(self, cache_service, mock_asyncpg_pool):
        """The cache key includes ``limit``: same name + different limit is a miss."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[{"id": 1, "name": "Stereolab", "score": 0.91}]
        )

        await cache_service.search_artists_by_name("Stereolab", limit=5)
        await cache_service.search_artists_by_name("Stereolab", limit=10)

        assert mock_asyncpg_pool._mock_conn.fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_result_not_cached(self, cache_service, mock_asyncpg_pool):
        """An empty list is falsy but distinct from None — keep the behaviour with
        ``async_cached``: only ``None`` is uncacheable, ``[]`` is a real answer that
        should be cached to avoid re-running the trigram scan."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(return_value=[])

        await cache_service.search_artists_by_name("Nonexistent")
        await cache_service.search_artists_by_name("Nonexistent")

        # Empty list is a legitimate result — cache it so the next call is free.
        assert mock_asyncpg_pool._mock_conn.fetch.await_count == 1


class TestSearchReleasesByTitle:
    @pytest.mark.asyncio
    async def test_returns_release_with_canonical_artist(self, cache_service, mock_asyncpg_pool):
        """Phase 1.7: fuzzy release-title hit returns canonical title + primary artist."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[
                {"id": 12345, "title": "DOGA", "artist": "Juana Molina", "score": 0.81},
            ]
        )
        results = await cache_service.search_releases_by_title("DOG")
        assert results == [
            {"id": 12345, "title": "DOGA", "artist": "Juana Molina", "score": 0.81},
        ]

    @pytest.mark.asyncio
    async def test_query_targets_release_title_trigram_with_extra_zero(
        self, cache_service, mock_asyncpg_pool
    ):
        """The SQL must use the trigram operator on release.title and pin extra=0."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(return_value=[])
        await cache_service.search_releases_by_title("Aluminum Tunes", limit=3)
        call = mock_asyncpg_pool._mock_conn.fetch.call_args
        sql = call.args[0]
        assert "release_artist" in sql
        assert "extra = 0" in sql
        assert "f_unaccent" in sql
        assert "%" in sql  # trigram operator
        assert call.args[1] == "Aluminum Tunes"
        assert call.args[2] == 3

    @pytest.mark.asyncio
    async def test_raises_cache_unavailable_on_pool_error(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(side_effect=RuntimeError("conn lost"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.search_releases_by_title("anything")

    @pytest.mark.asyncio
    async def test_query_canceled_maps_to_cache_unavailable(self, cache_service, mock_asyncpg_pool):
        """LML#815: bounded by the SET LOCAL statement_timeout — a runaway scan
        cancels as ``QueryCanceledError`` and degrades to ``CacheUnavailableError``
        (cache-only, never a 500), preserving the cancel as ``__cause__``."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            side_effect=asyncpg.QueryCanceledError("canceling statement due to statement timeout")
        )
        with pytest.raises(CacheUnavailableError) as exc_info:
            await cache_service.search_releases_by_title("anything")
        assert isinstance(exc_info.value.__cause__, asyncpg.QueryCanceledError)


class TestSearchTracksByTitle:
    @pytest.mark.asyncio
    async def test_returns_track_with_release_artist(self, cache_service, mock_asyncpg_pool):
        """Phase 1.7: fuzzy track-title hit returns canonical title + parent release artist."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            return_value=[
                {"id": 555, "title": "Back, Baby", "artist": "Jessica Pratt", "score": 0.92},
            ]
        )
        results = await cache_service.search_tracks_by_title("Back Baby")
        assert results == [
            {"id": 555, "title": "Back, Baby", "artist": "Jessica Pratt", "score": 0.92},
        ]

    @pytest.mark.asyncio
    async def test_query_targets_release_track_title_trigram(
        self, cache_service, mock_asyncpg_pool
    ):
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(return_value=[])
        await cache_service.search_tracks_by_title("la paradoja", limit=2)
        call = mock_asyncpg_pool._mock_conn.fetch.call_args
        sql = call.args[0]
        assert "release_track" in sql
        assert "release_artist" in sql
        assert "f_unaccent" in sql
        assert "%" in sql
        assert call.args[1] == "la paradoja"
        assert call.args[2] == 2

    @pytest.mark.asyncio
    async def test_raises_cache_unavailable_on_pool_error(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(side_effect=RuntimeError("conn lost"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.search_tracks_by_title("anything")

    @pytest.mark.asyncio
    async def test_query_canceled_maps_to_cache_unavailable(self, cache_service, mock_asyncpg_pool):
        """LML#815: bounded by the SET LOCAL statement_timeout — a runaway scan
        cancels as ``QueryCanceledError`` and degrades to ``CacheUnavailableError``
        (cache-only, never a 500), preserving the cancel as ``__cause__``."""
        mock_asyncpg_pool._mock_conn.fetch = AsyncMock(
            side_effect=asyncpg.QueryCanceledError("canceling statement due to statement timeout")
        )
        with pytest.raises(CacheUnavailableError) as exc_info:
            await cache_service.search_tracks_by_title("anything")
        assert isinstance(exc_info.value.__cause__, asyncpg.QueryCanceledError)


# ---------------------------------------------------------------------------
# Negative-result cache (LML#341 / A4)
# ---------------------------------------------------------------------------


class TestLookupNegativeHit:
    @pytest.mark.asyncio
    async def test_hit_returns_true_for_non_expired_row(self, cache_service, mock_asyncpg_pool):
        # Non-NULL fetchval means "found a non-expired row".
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=1)
        result = await cache_service.lookup_negative_hit("Stereolab", "Fuses", False)
        assert result is True
        # The lookup query must select the table and gate on TTL inline.
        sql = mock_asyncpg_pool.fetchval.call_args.args[0]
        assert "lookup_negative" in sql
        assert "attempted_at" in sql
        assert "ttl_seconds" in sql

    @pytest.mark.asyncio
    async def test_miss_returns_false_when_no_row(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=None)
        result = await cache_service.lookup_negative_hit("Stereolab", "Fuses", False)
        assert result is False

    @pytest.mark.asyncio
    async def test_distinguishes_artist_as_keyword_dimension(
        self, cache_service, mock_asyncpg_pool
    ):
        # The cache key must include `artist_as_keyword` so that
        # `q=Stereolab` (compilation search) does NOT hit a negative entry
        # written for the `artist=Stereolab` (field-filter) path. Verify
        # the two key_hashes differ.
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=None)
        await cache_service.lookup_negative_hit("Stereolab", "Fuses", False)
        await cache_service.lookup_negative_hit("Stereolab", "Fuses", True)
        # Both calls used key_hash as the WHERE-bound parameter (args[1]).
        keys = [call.args[1] for call in mock_asyncpg_pool.fetchval.call_args_list]
        assert keys[0] != keys[1], "artist_as_keyword must change the key_hash"

    @pytest.mark.asyncio
    async def test_normalized_for_case_and_diacritics(self, cache_service, mock_asyncpg_pool):
        # Two queries that differ only by case/diacritics on artist or track
        # should hash to the same key. Mirrors the existing
        # `make_normalized_cache_key` semantics used by the @async_cached
        # decorators (A5 / LML#272) so the negative cache lines up with
        # what the in-memory cache treats as "the same query."
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=None)
        await cache_service.lookup_negative_hit("Nilüfer Yanya", "Heavyweight Champion", False)
        await cache_service.lookup_negative_hit("nilufer yanya", "heavyweight champion", False)
        keys = [call.args[1] for call in mock_asyncpg_pool.fetchval.call_args_list]
        assert keys[0] == keys[1], "diacritic + case-only variations must collapse to one key"

    @pytest.mark.asyncio
    async def test_returns_false_on_pool_error_does_not_raise(
        self, cache_service, mock_asyncpg_pool
    ):
        # The negative cache is best-effort. A connection blip must NOT
        # block the request — fall through to the API as if the cache
        # said "no entry" rather than propagate the error.
        mock_asyncpg_pool.fetchval = AsyncMock(side_effect=RuntimeError("conn lost"))
        result = await cache_service.lookup_negative_hit("anything", "anywhere", False)
        assert result is False


class TestRecordLookupNegative:
    @pytest.mark.asyncio
    async def test_inserts_row_with_default_ttl(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.execute = AsyncMock(return_value="INSERT 0 1")
        await cache_service.record_lookup_negative("Stereolab", "Fuses", False)
        call = mock_asyncpg_pool.execute.call_args
        sql = call.args[0]
        assert "INSERT INTO lookup_negative" in sql
        # ON CONFLICT to refresh attempted_at on re-write so the TTL is reset.
        assert "ON CONFLICT" in sql.upper()
        # Default ttl_seconds=604800 (7 days).
        assert 604800 in call.args[1:], f"expected ttl_seconds=604800 among bound args: {call.args}"

    @pytest.mark.asyncio
    async def test_accepts_custom_ttl(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.execute = AsyncMock(return_value="INSERT 0 1")
        await cache_service.record_lookup_negative("X", "Y", False, ttl_seconds=3600)
        call = mock_asyncpg_pool.execute.call_args
        assert 3600 in call.args[1:]

    @pytest.mark.asyncio
    async def test_swallows_pool_error_does_not_raise(self, cache_service, mock_asyncpg_pool):
        # Write is best-effort. A pool blip must NOT surface to the caller
        # — at worst we miss the negative-cache write and the next request
        # for the same query pays the API cost again, exactly like today.
        mock_asyncpg_pool.execute = AsyncMock(side_effect=RuntimeError("conn lost"))
        # Should not raise.
        await cache_service.record_lookup_negative("anything", "anywhere", False)


# ---------------------------------------------------------------------------
# artist_equality_candidates (LML#759)
# ---------------------------------------------------------------------------


class TestArtistEqualityCandidates:
    """LML#759 tier-2 evidence legs, rewritten as candidate SETS.

    The reconciler's ``SELECT DISTINCT`` + dict-comprehension collapse
    silently picks one arbitrary candidate when a form is overloaded — it
    cannot honor "ambiguous names must not mint." These queries must return
    the full per-form id set for each equality leg instead.
    """

    @pytest.mark.asyncio
    async def test_empty_input_short_circuits_without_query(self, cache_service, mock_asyncpg_pool):
        assert await cache_service.artist_equality_candidates([]) == {}
        mock_asyncpg_pool.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_round_trip_with_forms_array(self, cache_service, mock_asyncpg_pool):
        """All four legs for the whole batch ride one query — the batched
        PG pre-pass budget in #759 is 1-3 round-trips for the request."""
        mock_asyncpg_pool.fetch = AsyncMock(return_value=[])
        await cache_service.artist_equality_candidates(["popsicle", "wishy"])
        mock_asyncpg_pool.fetch.assert_awaited_once()
        args = mock_asyncpg_pool.fetch.await_args.args
        assert args[1] == ["popsicle", "wishy"]

    @pytest.mark.asyncio
    async def test_overloaded_form_keeps_full_candidate_set(self, cache_service, mock_asyncpg_pool):
        """Two exact-leg ids for one form must BOTH come back — the
        anti-collapse property this method exists for."""
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[{"leg": "exact", "form": "popsicle", "artist_ids": [111, 222]}]
        )
        result = await cache_service.artist_equality_candidates(["popsicle"])
        assert result["popsicle"].exact == {111, 222}

    @pytest.mark.asyncio
    async def test_legs_route_to_their_own_sets(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {"leg": "exact", "form": "stereolab", "artist_ids": [4242]},
                {"leg": "member", "form": "laetitia sadier", "artist_ids": [200]},
                {"leg": "alias", "form": "stereolab", "artist_ids": [300]},
                {"leg": "name_variation", "form": "stereolab", "artist_ids": [400]},
            ]
        )
        result = await cache_service.artist_equality_candidates(["stereolab", "laetitia sadier"])
        assert result["stereolab"].exact == {4242}
        assert result["stereolab"].alias == {300}
        assert result["stereolab"].name_variation == {400}
        assert result["stereolab"].member == set()
        assert result["laetitia sadier"].member == {200}
        assert result["laetitia sadier"].exact == set()

    @pytest.mark.asyncio
    async def test_forms_without_rows_get_empty_sets(self, cache_service, mock_asyncpg_pool):
        """A miss is a measured zero on every leg — the key must exist so
        the resolver never confuses "no candidates" with "not queried"."""
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[{"leg": "exact", "form": "sessa", "artist_ids": [7]}]
        )
        result = await cache_service.artist_equality_candidates(["sessa", "wishy"])
        assert set(result.keys()) == {"sessa", "wishy"}
        assert result["wishy"].exact == set()
        assert result["wishy"].member == set()
        assert result["wishy"].alias == set()
        assert result["wishy"].name_variation == set()

    @pytest.mark.asyncio
    async def test_db_error_wrapped_as_cache_unavailable(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=RuntimeError("conn refused"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.artist_equality_candidates(["stereolab"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_row",
        [
            # A form the batch never asked for (= ANY invariant violated).
            {"leg": "exact", "form": "not in batch", "artist_ids": [1]},
            # A leg label with no dataclass field (SQL/dataclass drift).
            {"leg": "group", "form": "stereolab", "artist_ids": [1]},
        ],
    )
    async def test_invariant_violation_fails_loudly(
        self, cache_service, mock_asyncpg_pool, bad_row
    ):
        """A row violating the SQL↔dataclass invariants must raise (wrapped
        as CacheUnavailableError), never be silently skipped — a silent
        drop is how an ambiguous name reads as unique and mints wrong.
        Pins the direct-access contract so a future editor can't quietly
        restore ``.get(...) + continue`` defensiveness."""
        mock_asyncpg_pool.fetch = AsyncMock(return_value=[bad_row])
        with pytest.raises(CacheUnavailableError):
            await cache_service.artist_equality_candidates(["stereolab"])


# ---------------------------------------------------------------------------
# artist_trigram_candidates (LML#759)
# ---------------------------------------------------------------------------


class TestArtistEqualityCandidatesLegIteration:
    """The dataclass owns its leg enumeration (LML#759 review round 1):
    consumers derive corroboration and the conflict-rule union from ONE
    field list, so a leg added to the dataclass cannot be silently absent
    from either — a missing leg in the union weakens the veto and lets an
    ambiguous name mint."""

    def test_nonempty_legs_in_field_declaration_order(self):
        candidates = ArtistEqualityCandidates(
            exact={1},
            alias={3},
            name_variation={4},
        )
        assert candidates.nonempty_legs() == ["exact", "alias", "name_variation"]

    def test_nonempty_legs_empty_when_all_legs_measured_zero(self):
        assert ArtistEqualityCandidates().nonempty_legs() == []

    def test_leg_names_match_wire_enum_values(self):
        """Field names are contractually tied to ArtistResolveCacheLeg
        (each wire value is "cache_" + field name); a rename on either
        side must fail here, not silently desynchronize telemetry."""
        wire_values = {leg.value for leg in ArtistResolveCacheLeg}
        for f in fields(ArtistEqualityCandidates):
            assert f"cache_{f.name}" in wire_values

    def test_all_candidate_ids_unions_every_leg(self):
        candidates = ArtistEqualityCandidates(
            exact={1, 2},
            member={2, 3},
            alias={4},
            name_variation={5},
        )
        assert candidates.all_candidate_ids() == {1, 2, 3, 4, 5}

    def test_all_candidate_ids_empty_on_measured_zero(self):
        assert ArtistEqualityCandidates().all_candidate_ids() == set()

    def test_field_order_matches_wire_enum_declaration_order(self):
        """nonempty_legs() promises field order == wire enum order — a
        reorder on either side silently changes cache_corroboration
        ordering that downstream consumers treat as deterministic."""
        equality_enum_values = [
            leg.value
            for leg in ArtistResolveCacheLeg
            if leg is not ArtistResolveCacheLeg.cache_trigram
        ]
        assert [f"cache_{f.name}" for f in fields(ArtistEqualityCandidates)] == (
            equality_enum_values
        )

    def test_every_equality_enum_value_has_a_dataclass_field(self):
        """Reverse parity: a wire enum leg added without its dataclass
        field would silently never corroborate NOR veto — the exact
        missing-leg failure the leg-iteration methods exist to prevent."""
        field_names = {f.name for f in fields(ArtistEqualityCandidates)}
        for leg in ArtistResolveCacheLeg:
            if leg is ArtistResolveCacheLeg.cache_trigram:
                continue
            assert leg.value.removeprefix("cache_") in field_names


class TestArtistTrigramCandidates:
    """The trigram query runs on an acquired connection inside a transaction
    so ``SET LOCAL pg_trgm.similarity_threshold`` can pin the ``%``
    pre-filter floor — assertions target ``pool._mock_conn``."""

    @pytest.mark.asyncio
    async def test_empty_input_short_circuits_without_query(self, cache_service, mock_asyncpg_pool):
        assert await cache_service.artist_trigram_candidates([]) == {}
        mock_asyncpg_pool._mock_conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_batched_single_round_trip_keyed_by_input(self, cache_service, mock_asyncpg_pool):
        conn = mock_asyncpg_pool._mock_conn
        conn.fetch = AsyncMock(return_value=[{"input": "Nilüfer Yanya", "artist_ids": [5499521]}])
        result = await cache_service.artist_trigram_candidates(["Nilüfer Yanya", "Hot 8"])
        conn.fetch.assert_awaited_once()
        assert result == {"Nilüfer Yanya": {5499521}, "Hot 8": set()}

    @pytest.mark.asyncio
    async def test_default_threshold_bound_in_query(self, cache_service, mock_asyncpg_pool):
        conn = mock_asyncpg_pool._mock_conn
        conn.fetch = AsyncMock(return_value=[])
        await cache_service.artist_trigram_candidates(["Stereolab"])
        args = conn.fetch.await_args.args
        assert args[1] == ["Stereolab"]
        assert args[2] == pytest.approx(0.85)

    @pytest.mark.asyncio
    async def test_threshold_override_passed_through(self, cache_service, mock_asyncpg_pool):
        conn = mock_asyncpg_pool._mock_conn
        conn.fetch = AsyncMock(return_value=[])
        await cache_service.artist_trigram_candidates(["Stereolab"], threshold=0.4)
        args = conn.fetch.await_args.args
        assert args[2] == pytest.approx(0.4)

    @pytest.mark.asyncio
    async def test_pins_session_similarity_floor_in_transaction(
        self, cache_service, mock_asyncpg_pool
    ):
        """``pg_trgm.similarity_threshold`` is a GUC settable at server/
        database/role scope, so the module's 0.3-floor assumption is
        falsifiable by a DBA tuning the shared cache — the query must pin
        it with ``SET LOCAL`` inside its own transaction, BEFORE the fetch.

        The ordering is the contract: ``SET LOCAL`` outside a transaction
        is a WARNING no-op on real PostgreSQL, and the pg integration
        tests can't catch a hoist because the server default equals the
        pin — so this test records the event sequence explicitly.
        """
        conn = mock_asyncpg_pool._mock_conn
        events: list[str] = []
        tx_ctx = conn._mock_tx_ctx
        tx_ctx.__aenter__ = AsyncMock(side_effect=lambda: events.append("tx_enter") or tx_ctx)
        tx_ctx.__aexit__ = AsyncMock(side_effect=lambda *a: events.append("tx_exit") or False)

        async def record_execute(sql, *args):
            if "similarity_threshold" in sql:
                assert "SET LOCAL" in sql
                events.append("set_local")

        async def record_fetch(*args):
            events.append("fetch")
            return []

        conn.execute = AsyncMock(side_effect=record_execute)
        conn.fetch = AsyncMock(side_effect=record_fetch)
        await cache_service.artist_trigram_candidates(["Stereolab"])
        assert events == ["tx_enter", "set_local", "fetch", "tx_exit"]

    @pytest.mark.asyncio
    async def test_multi_id_candidate_set_not_collapsed(self, cache_service, mock_asyncpg_pool):
        conn = mock_asyncpg_pool._mock_conn
        conn.fetch = AsyncMock(return_value=[{"input": "Popsicle", "artist_ids": [5001, 5002]}])
        result = await cache_service.artist_trigram_candidates(["Popsicle"])
        assert result["Popsicle"] == {5001, 5002}

    @pytest.mark.asyncio
    async def test_threshold_below_pg_trgm_floor_raises(self, cache_service, mock_asyncpg_pool):
        """A threshold under pg_trgm's pinned ``similarity_threshold`` floor
        (0.3) would silently drop candidates in the gap — the ``%``
        operator pre-filters at the floor before ``similarity() >= $2`` runs.
        Reject loudly instead of under-returning."""
        with pytest.raises(ValueError, match="0.3"):
            await cache_service.artist_trigram_candidates(["Stereolab"], threshold=0.2)
        mock_asyncpg_pool._mock_conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_bad_threshold_raises_even_on_empty_batch(self, cache_service, mock_asyncpg_pool):
        """The guard must fire before the empty-batch short-circuit: a
        misconfigured threshold constant should fail a dry-run whose batch
        happens to be empty, not surface mid-way through the first real one."""
        with pytest.raises(ValueError, match="0.3"):
            await cache_service.artist_trigram_candidates([], threshold=0.2)

    @pytest.mark.asyncio
    async def test_db_error_wrapped_as_cache_unavailable(self, cache_service, mock_asyncpg_pool):
        conn = mock_asyncpg_pool._mock_conn
        conn.fetch = AsyncMock(side_effect=RuntimeError("conn refused"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.artist_trigram_candidates(["Stereolab"])

    @pytest.mark.asyncio
    async def test_unknown_input_row_fails_loudly(self, cache_service, mock_asyncpg_pool):
        """A row keyed by an input the batch never sent (unnest invariant
        violated) must raise, not be silently skipped — same loud-failure
        contract as the equality legs."""
        conn = mock_asyncpg_pool._mock_conn
        conn.fetch = AsyncMock(return_value=[{"input": "not in batch", "artist_ids": [1]}])
        with pytest.raises(CacheUnavailableError):
            await cache_service.artist_trigram_candidates(["Stereolab"])


# ---------------------------------------------------------------------------
# get_release_lean / get_artist_details_lean (LML#894, lever L4a)
#
# The lean /lookup-only hydration read path omits the 4 children /lookup never
# consumes (release_video; artist_alias, artist_name_variation, artist_member)
# to cut PG round-trips 14->10. The shared get_release / get_artist_details
# MUST keep reading those children so the public /api/v1/discogs/* responses
# stay full on both warm and cold paths (the plans/lookup-latency-plan.md
# section 6.3 fence). release_track_artist (LML#699 writer credits) stays on
# the lean release path.
# ---------------------------------------------------------------------------


def _capture_release_row():
    return {
        "id": 1,
        "title": "Aluminum Tunes",
        "release_year": 1998,
        "artwork_url": "https://img.com/a.jpg",
        "released": None,
        "artwork_checked_at": None,
        "not_found": False,
        "master_id": None,
    }


class TestGetReleaseLean:
    @pytest.mark.asyncio
    async def test_lean_skips_release_video(self, cache_service, mock_asyncpg_pool):
        """get_release_lean must NOT query release_video, but MUST still read
        the tracklist / artist / label / genre / style children AND both the
        extra=0 and extra=1 release_track_artist legs (LML#699 writer credits).
        """
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=_capture_release_row())
        captured_queries: list[str] = []

        async def capture_fetch(query, *args):
            captured_queries.append(query)
            return []

        mock_asyncpg_pool.fetch = AsyncMock(side_effect=capture_fetch)

        result = await cache_service.get_release_lean(1)
        assert result is not None

        joined = [" ".join(q.split()) for q in captured_queries]
        assert not any("release_video" in q for q in joined), (
            f"lean release path must not read release_video; got: {joined!r}"
        )
        # Kept children.
        for table in (
            "release_artist",
            "release_label",
            "release_track",
            "release_genre",
            "release_style",
        ):
            assert any(table in q for q in joined), (
                f"lean release path must still read {table}; got: {joined!r}"
            )
        # LML#699: both release_track_artist legs survive on the lean path.
        rta = [q for q in joined if "release_track_artist" in q]
        assert any("AND extra = 0" in q for q in rta), (
            f"lean path must keep the extra=0 release_track_artist read; got: {rta!r}"
        )
        assert any("AND extra = 1" in q for q in rta), (
            f"lean path must keep the extra=1 writer-credit read (LML#699); got: {rta!r}"
        )

    @pytest.mark.asyncio
    async def test_lean_returns_empty_videos(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=_capture_release_row())
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[
                    {"position": "1", "title": "Space Moment", "duration": None, "sequence": 1}
                ],
                release_artist=[
                    {"artist_id": 42, "artist_name": "Stereolab", "extra": 0, "role": None}
                ],
                release_label=[{"label_id": 7, "label_name": "Duophonic", "catno": "D-1"}],
                release_genre=[{"genre": "Rock"}],
                release_style=[{"style": "Post-Rock"}],
            )
        )

        result = await cache_service.get_release_lean(1)
        assert result is not None
        assert result.videos == []
        # Core /lookup-consumed fields are intact.
        assert result.title == "Aluminum Tunes"
        assert result.artist == "Stereolab"
        assert result.artist_id == 42
        assert result.genres == ["Rock"]
        assert result.styles == ["Post-Rock"]
        assert result.label == "Duophonic"
        assert len(result.tracklist) == 1
        assert result.cached is True

    @pytest.mark.asyncio
    async def test_lean_shape_parity_with_full_for_videoless_release(
        self, cache_service, mock_asyncpg_pool
    ):
        """For a release with no videos, get_release_lean returns the same core
        payload get_release does (the /lookup-visible surface is unchanged)."""
        router = make_fetch_router(
            release_track_artist=[{"track_sequence": 1, "artist_name": "Stereolab"}],
            release_track=[
                {"position": "1", "title": "Space Moment", "duration": None, "sequence": 1}
            ],
            release_artist=[
                {"artist_id": 42, "artist_name": "Stereolab", "extra": 0, "role": None}
            ],
            release_label=[{"label_id": 7, "label_name": "Duophonic", "catno": "D-1"}],
            release_genre=[{"genre": "Rock"}],
            release_style=[{"style": "Post-Rock"}],
            release_video=[],
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=_capture_release_row())
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=router)
        full = await cache_service.get_release(1)

        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=_capture_release_row())
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=router)
        lean = await cache_service.get_release_lean(1)

        assert full is not None and lean is not None
        assert lean.title == full.title
        assert lean.artist == full.artist
        assert lean.artist_id == full.artist_id
        assert lean.genres == full.genres
        assert lean.styles == full.styles
        assert lean.label == full.label
        assert [t.title for t in lean.tracklist] == [t.title for t in full.tracklist]
        assert [t.artists for t in lean.tracklist] == [t.artists for t in full.tracklist]
        assert lean.videos == full.videos == []

    @pytest.mark.asyncio
    async def test_shared_get_release_still_reads_release_video(
        self, cache_service, mock_asyncpg_pool
    ):
        """Regression fence: the shared get_release path (serves /discogs/*) must
        keep querying release_video so warm hits are not empty vs cold full."""
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=_capture_release_row())
        captured_queries: list[str] = []

        async def capture_fetch(query, *args):
            captured_queries.append(query)
            return []

        mock_asyncpg_pool.fetch = AsyncMock(side_effect=capture_fetch)
        await cache_service.get_release(1)
        assert any("release_video" in q for q in captured_queries), (
            "shared get_release must still read release_video (LML#894 fence); "
            f"got: {captured_queries!r}"
        )

    @pytest.mark.asyncio
    async def test_lean_not_found_returns_none(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=None)
        assert await cache_service.get_release_lean(999) is None

    @pytest.mark.asyncio
    async def test_lean_error_raises_cache_unavailable(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(side_effect=Exception("db error"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.get_release_lean(1)


def _capture_artist_row():
    from datetime import datetime

    return {
        "id": 77,
        "name": "Stereolab",
        "profile": "Anglo-French band.",
        "image_url": "https://i.discogs.com/stereolab.jpg",
        "fetched_at": datetime(2026, 1, 1, tzinfo=UTC),
        "not_found": False,
    }


class TestGetArtistDetailsLean:
    @pytest.mark.asyncio
    async def test_lean_skips_related_children(self, cache_service, mock_asyncpg_pool):
        """get_artist_details_lean must NOT query artist_alias /
        artist_name_variation / artist_member, but MUST still read artist_url
        (the /lookup wikipedia-URL surface)."""
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=_capture_artist_row())
        captured_queries: list[str] = []

        async def capture_fetch(query, *args):
            captured_queries.append(query)
            return []

        mock_asyncpg_pool.fetch = AsyncMock(side_effect=capture_fetch)
        result = await cache_service.get_artist_details_lean(77)
        assert result is not None

        for table in ("artist_alias", "artist_name_variation", "artist_member"):
            assert not any(table in q for q in captured_queries), (
                f"lean artist path must not read {table}; got: {captured_queries!r}"
            )
        assert any("artist_url" in q for q in captured_queries), (
            f"lean artist path must still read artist_url; got: {captured_queries!r}"
        )

    @pytest.mark.asyncio
    async def test_lean_returns_empty_related_but_keeps_urls_and_profile(
        self, cache_service, mock_asyncpg_pool
    ):
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=_capture_artist_row())
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                artist_url=[{"url": "https://en.wikipedia.org/wiki/Stereolab"}],
            )
        )
        result = await cache_service.get_artist_details_lean(77)
        assert result is not None
        assert result.name == "Stereolab"
        assert result.profile == "Anglo-French band."
        assert result.aliases == []
        assert result.name_variations == []
        assert result.members == []
        assert result.urls == ["https://en.wikipedia.org/wiki/Stereolab"]
        assert result.cached is True

    @pytest.mark.asyncio
    async def test_shared_get_artist_details_still_reads_children(
        self, cache_service, mock_asyncpg_pool
    ):
        """Regression fence: the shared get_artist_details path (serves
        /discogs/*) must keep reading all three related-child tables."""
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=_capture_artist_row())
        captured_queries: list[str] = []

        async def capture_fetch(query, *args):
            captured_queries.append(query)
            return []

        mock_asyncpg_pool.fetch = AsyncMock(side_effect=capture_fetch)
        await cache_service.get_artist_details(77)
        for table in ("artist_alias", "artist_name_variation", "artist_member", "artist_url"):
            assert any(table in q for q in captured_queries), (
                f"shared get_artist_details must still read {table} (LML#894 fence); "
                f"got: {captured_queries!r}"
            )

    @pytest.mark.asyncio
    async def test_lean_not_found_returns_none(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=None)
        assert await cache_service.get_artist_details_lean(999) is None

    @pytest.mark.asyncio
    async def test_lean_error_raises_cache_unavailable(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(side_effect=Exception("db error"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.get_artist_details_lean(77)
