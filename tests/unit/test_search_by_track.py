"""Unit tests for the album-by-track endpoint and cache method.

Covers:
  - ``DiscogsCacheService.search_albums_by_track_title``: SQL parameters,
    return-shape projection, threshold pass-through, error wrapping.
  - ``GET /api/v1/discogs/search-by-track``: success, query-param plumbing,
    503 when cache is unconfigured, graceful-empty on cache errors,
    422 on missing query.
"""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from tests.unit.conftest import override_deps

# ---------------------------------------------------------------------------
# Cache method: search_albums_by_track_title
# ---------------------------------------------------------------------------


class _MockTxn:
    """Async context manager standing in for ``conn.transaction()``."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class _MockAcquire:
    """Async context manager standing in for ``pool.acquire()``."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        return None


def _make_cache_svc(fetch_rows: list[dict]) -> tuple[DiscogsCacheService, AsyncMock]:
    """Build a ``DiscogsCacheService`` whose ``pool.acquire()`` yields a
    connection mock with ``transaction``/``fetchval``/``fetch`` wired up.

    Returns ``(svc, mock_conn)`` so tests can assert on the underlying
    connection's method calls.
    """
    mock_conn = AsyncMock()
    mock_conn.transaction = lambda: _MockTxn()
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.fetch = AsyncMock(return_value=fetch_rows)

    mock_pool = AsyncMock()
    mock_pool.acquire = lambda: _MockAcquire(mock_conn)

    svc = DiscogsCacheService.__new__(DiscogsCacheService)
    svc.pool = mock_pool
    return svc, mock_conn


class TestSearchAlbumsByTrackTitle:
    """Direct tests of the cache method.

    Mocks ``pool.acquire`` so we can assert on the parametrised SQL call
    without spinning a real Postgres. The method opens a transaction +
    binds ``pg_trgm.similarity_threshold`` before fetching, so the mocked
    connection needs ``transaction()`` / ``fetchval()`` / ``fetch()``.
    """

    @pytest.mark.asyncio
    async def test_returns_projected_dicts(self):
        svc, _ = _make_cache_svc(
            [
                {
                    "release_id": 1573110,
                    "master_id": 1374,
                    "release_title": "Confield",
                    "release_artist": "Autechre",
                    "track_title": "VI Scose Poise",
                    "track_position": "1",
                    "score": 1.0,
                },
            ]
        )

        result = await svc.search_albums_by_track_title("vi scose poise")

        assert result == [
            {
                "release_id": 1573110,
                "master_id": 1374,
                "release_title": "Confield",
                "release_artist": "Autechre",
                "track_title": "VI Scose Poise",
                "track_position": "1",
                "score": 1.0,
            }
        ]

    @pytest.mark.asyncio
    async def test_passes_query_limit_and_threshold_to_pg(self):
        svc, conn = _make_cache_svc([])
        await svc.search_albums_by_track_title("scose poise", limit=10, score_threshold=0.5)
        # Positional args to fetch: (sql, query, threshold, limit).
        call_args = conn.fetch.await_args.args
        assert call_args[1] == "scose poise"
        assert call_args[2] == 0.5
        assert call_args[3] == 10

    @pytest.mark.asyncio
    async def test_defaults_threshold_to_pg_trgm_default(self):
        svc, conn = _make_cache_svc([])
        await svc.search_albums_by_track_title("anything")
        call_args = conn.fetch.await_args.args
        assert call_args[2] == 0.3  # pg_trgm default
        assert call_args[3] == 50  # function default

    @pytest.mark.asyncio
    async def test_coerces_score_to_float(self):
        # asyncpg's Real → Python decimal-like type would surface as Decimal in
        # some installs; the projection coerces to plain float so callers can
        # rely on the type.
        svc, _ = _make_cache_svc(
            [
                {
                    "release_id": 1,
                    "master_id": None,
                    "release_title": "X",
                    "release_artist": "Y",
                    "track_title": "T",
                    "track_position": "1",
                    "score": 0.5,
                }
            ]
        )
        result = await svc.search_albums_by_track_title("anything")
        assert isinstance(result[0]["score"], float)

    @pytest.mark.asyncio
    async def test_master_id_passes_through_null(self):
        # Releases with no master_id (singles, demos, V/A one-offs) come back
        # with master_id NULL. The projection preserves None.
        svc, _ = _make_cache_svc(
            [
                {
                    "release_id": 99,
                    "master_id": None,
                    "release_title": "Untitled",
                    "release_artist": "Various",
                    "track_title": "Track",
                    "track_position": "A",
                    "score": 0.7,
                }
            ]
        )
        result = await svc.search_albums_by_track_title("track")
        assert result[0]["master_id"] is None

    @pytest.mark.asyncio
    async def test_null_track_position_passes_through(self):
        # ``release_track.position`` is nullable in the Discogs schema. The
        # projection must preserve None; the Pydantic model handles it
        # downstream (see endpoint test).
        svc, _ = _make_cache_svc(
            [
                {
                    "release_id": 9999,
                    "master_id": None,
                    "release_title": "Digital Only",
                    "release_artist": "Some Artist",
                    "track_title": "Track",
                    "track_position": None,
                    "score": 0.9,
                }
            ]
        )
        result = await svc.search_albums_by_track_title("track")
        assert result[0]["track_position"] is None

    @pytest.mark.asyncio
    async def test_sets_pg_trgm_similarity_threshold_per_query(self):
        """The ``%`` operator in the WHERE clause reads ``pg_trgm.similarity_threshold``
        (a session GUC defaulting to 0.3). Without binding the per-call
        threshold, callers requesting < 0.3 silently get 0.3 behavior because
        the operator's GIN-index lookup floors at the GUC value regardless of
        the explicit ``similarity(...) >= $2`` clause.

        The cache method must run the query inside a transaction that
        ``SELECT set_config('pg_trgm.similarity_threshold', $1, true)`` to
        make the operator floor match the caller's request. ``is_local =
        true`` scopes the change to the transaction so concurrent queries on
        other pool connections aren't affected."""
        svc, conn = _make_cache_svc([])

        await svc.search_albums_by_track_title("vi scose poise", score_threshold=0.15)

        # Verify the connection bound the threshold inside the transaction.
        set_config_calls = [
            call
            for call in conn.fetchval.await_args_list
            if call.args and "set_config" in str(call.args[0]).lower()
        ]
        assert set_config_calls, (
            "expected the cache method to bind pg_trgm.similarity_threshold via "
            "set_config(...) inside the transaction; otherwise the `%` operator's "
            "GUC default (0.3) silently overrides the caller's threshold."
        )
        # And that the binding used the per-call value with is_local=true.
        call = set_config_calls[0]
        assert call.args[1] == "0.15"
        assert call.args[2] is True

    @pytest.mark.asyncio
    async def test_wraps_db_errors_as_cache_unavailable(self):
        svc, conn = _make_cache_svc([])
        conn.fetch = AsyncMock(side_effect=RuntimeError("connection refused"))

        with pytest.raises(CacheUnavailableError) as exc_info:
            await svc.search_albums_by_track_title("anything")
        assert "connection refused" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_collab_release_artist_is_alphabetically_first(self):
        """``DISTINCT ON (rt.release_id)`` plus ``ORDER BY rt.release_id,
        score DESC, ra.artist_name ASC`` must make collab-album results
        deterministic. Without the alphabetical tiebreaker, the surviving
        ``release_artist`` depends on PG's storage-order encounter for the
        ``release_artist`` JOIN — same query, same data could yield "Duke
        Ellington" one run and "John Coltrane" another (after vacuum, after
        an index rebuild, after a different join order chosen by the
        planner).

        We assert the SQL the cache method emits orders by
        ``ra.artist_name ASC`` as the final tiebreaker. The pg-integration
        test pins the end-to-end behavior.
        """
        svc, conn = _make_cache_svc([])
        await svc.search_albums_by_track_title("anything")
        # `conn.fetch` is called with (sql, query, threshold, limit). Assert
        # the SQL contains the alphabetical tiebreaker — implementation
        # detail, but the alternative (matching against actual PG output)
        # would require the integration suite and we want a unit-level
        # regression guard.
        sql = conn.fetch.await_args.args[0]
        # Look for the tiebreaker in the inner DISTINCT ON ordering.
        assert "ra.artist_name asc" in sql.lower(), (
            "Expected `ra.artist_name ASC` as the final ORDER BY clause inside "
            "DISTINCT ON to make collab-album results deterministic. Without it, "
            "the surviving artist row is storage-order-dependent."
        )


# ---------------------------------------------------------------------------
# Endpoint: GET /api/v1/discogs/search-by-track
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_discogs():
    from discogs.service import DiscogsService

    return AsyncMock(spec=DiscogsService)


@pytest.fixture
def mock_cache():
    return AsyncMock(spec=DiscogsCacheService)


@pytest.fixture
def app_with_cache(mock_discogs, mock_cache, mock_settings):
    from config.settings import get_settings
    from core.dependencies import (
        get_discogs_cache_service,
        get_discogs_service,
        get_library_db,
        get_posthog_client,
    )
    from main import app

    with override_deps(
        app,
        {
            get_library_db: AsyncMock(),
            get_discogs_service: mock_discogs,
            get_discogs_cache_service: mock_cache,
            get_posthog_client: None,
            get_settings: mock_settings,
        },
    ):
        yield app


@pytest.fixture
def app_without_cache(mock_discogs, mock_settings):
    from config.settings import get_settings
    from core.dependencies import (
        get_discogs_cache_service,
        get_discogs_service,
        get_library_db,
        get_posthog_client,
    )
    from main import app

    with override_deps(
        app,
        {
            get_library_db: AsyncMock(),
            get_discogs_service: mock_discogs,
            get_discogs_cache_service: None,
            get_posthog_client: None,
            get_settings: mock_settings,
        },
    ):
        yield app


class TestSearchByTrackEndpoint:
    @pytest.mark.asyncio
    async def test_success(self, app_with_cache, mock_cache):
        mock_cache.search_albums_by_track_title = AsyncMock(
            return_value=[
                {
                    "release_id": 1573110,
                    "master_id": 1374,
                    "release_title": "Confield",
                    "release_artist": "Autechre",
                    "track_title": "VI Scose Poise",
                    "track_position": "1",
                    "score": 1.0,
                },
                {
                    "release_id": 8434,
                    "master_id": 1374,
                    "release_title": "Confield",
                    "release_artist": "Autechre",
                    "track_title": "VI Scose Poise",
                    "track_position": "A1",
                    "score": 1.0,
                },
            ]
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "vi scose poise"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["query"] == "vi scose poise"
        assert {r["release_id"] for r in body["results"]} == {1573110, 8434}
        # Backend will join these release_ids against
        # ``library.canonical_entity_id = 'discogs:' || release_id``.
        assert all(r["release_artist"] == "Autechre" for r in body["results"])

    @pytest.mark.asyncio
    async def test_query_params_pass_through_to_cache(self, app_with_cache, mock_cache):
        mock_cache.search_albums_by_track_title = AsyncMock(return_value=[])

        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "track", "limit": 25, "score_threshold": 0.6},
            )

        assert resp.status_code == 200
        mock_cache.search_albums_by_track_title.assert_called_once_with(
            "track", limit=25, score_threshold=0.6
        )

    @pytest.mark.asyncio
    async def test_missing_query_returns_422(self, app_with_cache):
        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/discogs/search-by-track")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_threshold_out_of_range_returns_422(self, app_with_cache):
        # FastAPI's Query(ge=0, le=1) validation. Use a valid-length q so
        # the 422 is unambiguously about the threshold, not min_length.
        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "track", "score_threshold": 1.5},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_no_cache_returns_503(self, app_without_cache):
        async with AsyncClient(
            transport=ASGITransport(app=app_without_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "anything"},
            )
        assert resp.status_code == 503
        assert "DATABASE_URL_DISCOGS" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_null_track_position_round_trips(self, app_with_cache, mock_cache):
        """``release_track.position`` is nullable in the Discogs schema (digital
        releases and some compilations omit it). The response model must accept
        ``track_position: None`` so the endpoint doesn't return 500 on those
        rows."""
        mock_cache.search_albums_by_track_title = AsyncMock(
            return_value=[
                {
                    "release_id": 9999,
                    "master_id": None,
                    "release_title": "Digital Only",
                    "release_artist": "Some Artist",
                    "track_title": "Track With No Position",
                    "track_position": None,
                    "score": 0.95,
                }
            ]
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "track with no position"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["results"][0]["track_position"] is None

    @pytest.mark.asyncio
    async def test_threshold_below_pg_trgm_default_is_admitted(self, app_with_cache, mock_cache):
        """``score_threshold=0.1`` must reach the cache layer (which is
        responsible for binding ``pg_trgm.similarity_threshold`` for the
        query). The API parameter accepts ``ge=0``; rejecting low thresholds
        at the API layer would mask the real issue (without SET-LOCAL
        plumbing in the cache, the GUC's 0.3 default short-circuits ``%`` and
        callers get 0.3 behavior regardless)."""
        mock_cache.search_albums_by_track_title = AsyncMock(return_value=[])

        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "track", "score_threshold": 0.1},
            )

        assert resp.status_code == 200
        mock_cache.search_albums_by_track_title.assert_called_once_with(
            "track", limit=50, score_threshold=0.1
        )

    @pytest.mark.asyncio
    async def test_q_is_trimmed_before_reaching_cache(self, app_with_cache, mock_cache):
        """Whitespace padding on the query is stripped at the route layer
        before calling the cache. Trigram comparison is whitespace-resilient,
        but trimming keeps the projected ``query`` field tidy and removes a
        no-op-but-confusing input variant."""
        mock_cache.search_albums_by_track_title = AsyncMock(return_value=[])

        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "  vi scose poise  "},
            )

        assert resp.status_code == 200
        # Trimmed before the cache call.
        mock_cache.search_albums_by_track_title.assert_called_once_with(
            "vi scose poise", limit=50, score_threshold=0.3
        )
        # And the response echoes the trimmed query, not the original.
        assert resp.json()["query"] == "vi scose poise"

    @pytest.mark.asyncio
    async def test_empty_query_returns_422(self, app_with_cache):
        """Empty string ``q`` is rejected. The trigram operator on an empty
        string is meaningless; without a minimum length, callers could submit
        ``q=""`` with low ``score_threshold`` and pull a massive scan. The
        route's ``min_length`` validator catches this at the API layer."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": ""},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_query_shorter_than_min_length_returns_422(self, app_with_cache):
        """``q`` shorter than 3 characters is rejected. Trigrams (3-char
        substrings) lose their selectivity below this length — a 1- or 2-char
        query matches a large fraction of any tracklist, so allowing it
        creates a cheap DOS vector against the cache. The auth gate
        (``LML_API_KEY``) is a secondary defense; the min-length enforcement
        is the first line."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "vi"},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_whitespace_only_query_returns_422(self, app_with_cache):
        """A whitespace-only query trims to empty, hitting the min-length
        rejection. The route trims FIRST, then validates."""
        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "   "},
            )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_cache_error_returns_empty_results_not_500(self, app_with_cache, mock_cache):
        """Mirror the autocomplete endpoint's graceful-empty behavior:
        cache errors don't bubble to the caller; the empty body is the
        signal."""
        mock_cache.search_albums_by_track_title = AsyncMock(
            side_effect=CacheUnavailableError("transient connection failure")
        )

        async with AsyncClient(
            transport=ASGITransport(app=app_with_cache), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/v1/discogs/search-by-track",
                params={"q": "anything"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["results"] == []
        assert body["total"] == 0
        assert body["query"] == "anything"
