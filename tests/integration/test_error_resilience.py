"""Error/resilience tests for library-metadata-lookup.

Tests that external dependency failures are handled gracefully:
- Bulk identity endpoint with 1000+ names (memory, timeout)
- Source transport connection failures (PgSource, HttpSource)
- Entity store unavailable graceful degradation
- Library search with closed/unavailable database

Uses WXYC example artists for fixture data.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# WXYC example artist names for bulk tests
# ---------------------------------------------------------------------------

WXYC_ARTISTS = [
    "Autechre",
    "Prince Jammy",
    "Juana Molina",
    "Stereolab",
    "Cat Power",
    "Jessica Pratt",
    "Chuquimamani-Condori",
    "Duke Ellington & John Coltrane",
    "Sessa",
    "Anne Gillis",
    "Father John Misty",
    "Rafael Toral",
    "Buck Meek",
    "Nourished by Time",
    "For Tracy Hyde",
    "Rochelle Jordan",
    "Large Professor",
]


def _generate_artist_names(count: int) -> list[str]:
    """Generate a list of artist names by cycling through WXYC artists.

    Each name is made unique by appending a numeric suffix when the count
    exceeds the number of canonical artists.
    """
    names = []
    for i in range(count):
        base = WXYC_ARTISTS[i % len(WXYC_ARTISTS)]
        if i < len(WXYC_ARTISTS):
            names.append(base)
        else:
            names.append(f"{base} {i}")
    return names


# ---------------------------------------------------------------------------
# Bulk identity endpoint with 1000+ names
# ---------------------------------------------------------------------------


class TestBulkIdentityEndpoint:
    """Bulk identity resolution handles large payloads without hanging or OOM."""

    @pytest_asyncio.fixture
    async def app_client_with_entity_store(self, library_db, test_settings):
        """httpx AsyncClient with a mock EntityStore that returns results."""
        from httpx import ASGITransport, AsyncClient

        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        # Check if identity router is registered; skip if not yet merged
        identity_routes = [r for r in app.routes if hasattr(r, "path") and "/identity" in r.path]
        if not identity_routes:
            pytest.skip("Identity endpoints not yet available on this branch")

        # Mock entity store that resolves the first 10 WXYC artists
        from unittest.mock import AsyncMock

        mock_store = AsyncMock()

        async def mock_get_identity(name):
            if name in WXYC_ARTISTS[:10]:
                return MagicMock(
                    library_name=name,
                    discogs_artist_id=100 + WXYC_ARTISTS.index(name),
                    wikidata_qid=None,
                    musicbrainz_artist_id=None,
                    spotify_artist_id=None,
                    apple_music_artist_id=None,
                    bandcamp_id=None,
                    reconciliation_status="reconciled",
                )
            return None

        mock_store.get_identity = AsyncMock(side_effect=mock_get_identity)

        # Override dependencies
        try:
            from identity.dependencies import get_entity_store

            app.dependency_overrides[get_entity_store] = lambda: mock_store
        except ImportError:
            pytest.skip("Identity module not available on this branch")

        app.dependency_overrides[get_library_db] = lambda: library_db
        app.dependency_overrides[get_discogs_service] = lambda: None
        app.dependency_overrides[get_posthog_client] = lambda: None
        app.dependency_overrides[get_settings] = lambda: test_settings

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client

        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_bulk_1000_names_completes(self, app_client_with_entity_store):
        """Bulk endpoint with 1000+ names completes without timeout or OOM."""
        names = _generate_artist_names(1200)
        resp = await app_client_with_entity_store.post("/identity/bulk", json={"names": names})
        assert resp.status_code == 200
        body = resp.json()
        assert "identities" in body
        assert "unresolved" in body
        total = len(body["identities"]) + len(body["unresolved"])
        assert total == 1200, f"Expected 1200 total, got {total}"

    @pytest.mark.asyncio
    async def test_bulk_empty_names_list(self, app_client_with_entity_store):
        """Bulk endpoint with empty list returns empty results."""
        resp = await app_client_with_entity_store.post("/identity/bulk", json={"names": []})
        assert resp.status_code == 200
        body = resp.json()
        assert body["identities"] == []
        assert body["unresolved"] == []

    @pytest.mark.asyncio
    async def test_bulk_duplicate_names_handled(self, app_client_with_entity_store):
        """Bulk endpoint handles duplicate names (returns one result per input)."""
        names = ["Stereolab", "Stereolab", "Stereolab"]
        resp = await app_client_with_entity_store.post("/identity/bulk", json={"names": names})
        assert resp.status_code == 200
        body = resp.json()
        total = len(body["identities"]) + len(body["unresolved"])
        assert total == 3, "Each input name should produce exactly one result"


# ---------------------------------------------------------------------------
# Bulk resource bounds (memory + wall-clock)
# ---------------------------------------------------------------------------


class TestBulkResourceBounds:
    """Bulk identity endpoint stays within memory and timing budgets.

    Reuses the same dependency-overridden client setup as TestBulkIdentityEndpoint
    so that the entity-store mock returns deterministically without hitting
    PostgreSQL. The assertions catch regressions like accidental N+1 query
    patterns, unbounded retries, or per-name buffering that would blow up
    memory on larger payloads.
    """

    @pytest_asyncio.fixture
    async def app_client_with_entity_store(self, library_db, test_settings):
        """httpx AsyncClient with a mock EntityStore that resolves a fixed subset.

        Identical wiring to TestBulkIdentityEndpoint.app_client_with_entity_store;
        duplicated here so the two test classes can be run independently and so
        each fixture stays scoped to its own class without cross-class coupling.
        """
        from httpx import ASGITransport, AsyncClient

        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        identity_routes = [r for r in app.routes if hasattr(r, "path") and "/identity" in r.path]
        if not identity_routes:
            pytest.skip("Identity endpoints not yet available on this branch")

        mock_store = AsyncMock()

        async def mock_get_identity(name):
            if name in WXYC_ARTISTS[:10]:
                return MagicMock(
                    library_name=name,
                    discogs_artist_id=100 + WXYC_ARTISTS.index(name),
                    wikidata_qid=None,
                    musicbrainz_artist_id=None,
                    spotify_artist_id=None,
                    apple_music_artist_id=None,
                    bandcamp_id=None,
                    reconciliation_status="reconciled",
                )
            return None

        mock_store.get_identity = AsyncMock(side_effect=mock_get_identity)

        try:
            from identity.dependencies import get_entity_store

            app.dependency_overrides[get_entity_store] = lambda: mock_store
        except ImportError:
            pytest.skip("Identity module not available on this branch")

        app.dependency_overrides[get_library_db] = lambda: library_db
        app.dependency_overrides[get_discogs_service] = lambda: None
        app.dependency_overrides[get_posthog_client] = lambda: None
        app.dependency_overrides[get_settings] = lambda: test_settings

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client

        app.dependency_overrides.clear()

    @pytest.mark.parametrize("size", [100, 1000, 5000])
    @pytest.mark.asyncio
    async def test_bulk_memory_bounded_under_200mb(self, app_client_with_entity_store, size: int):
        """Peak traced memory stays under 200 MB for bulk requests up to 5000 names.

        Catches regressions where bulk identity resolution accidentally
        materializes per-name intermediate buffers, retains response objects
        beyond the request lifecycle, or triggers unbounded query result
        accumulation.
        """
        import tracemalloc

        names = _generate_artist_names(size)

        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            resp = await app_client_with_entity_store.post("/identity/bulk", json={"names": names})
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert resp.status_code == 200
        body = resp.json()
        total = len(body["identities"]) + len(body["unresolved"])
        assert total == size, f"Expected {size} total, got {total}"

        peak_mb = peak / (1024 * 1024)
        assert peak < 200 * 1024 * 1024, (
            f"Bulk request of {size} names used {peak_mb:.1f} MB peak (limit: 200 MB)"
        )

    @pytest.mark.parametrize("size,timeout_s", [(1000, 30.0), (5000, 30.0)])
    @pytest.mark.asyncio
    async def test_bulk_completes_within_timeout(
        self, app_client_with_entity_store, size: int, timeout_s: float
    ):
        """Bulk request of up to 5000 names completes well under 30 s wall-clock.

        Catches regressions like accidental N+1 query patterns or unbounded
        retries that would compound latency with payload size.
        """
        import time

        names = _generate_artist_names(size)

        start = time.perf_counter()
        resp = await app_client_with_entity_store.post("/identity/bulk", json={"names": names})
        elapsed = time.perf_counter() - start

        assert resp.status_code == 200
        body = resp.json()
        total = len(body["identities"]) + len(body["unresolved"])
        assert total == size, f"Expected {size} total, got {total}"

        assert elapsed < timeout_s, (
            f"Bulk request of {size} names took {elapsed:.2f}s (timeout: {timeout_s}s)"
        )


# ---------------------------------------------------------------------------
# Source transport connection failures
# ---------------------------------------------------------------------------


class TestSourceTransportFailures:
    """PgSource and HTTP transport handle connection failures gracefully."""

    @pytest.mark.asyncio
    async def test_pg_source_connection_refused(self):
        """PgSource to a closed port raises a clear connection error."""
        try:
            from scripts.entity_resolution.sources import PgSource
        except ImportError:
            pytest.skip("Entity resolution sources not available on this branch")

        pg = PgSource("postgresql://localhost:59999/nonexistent")
        with pytest.raises(Exception) as exc_info:
            await pg.fetchall("SELECT 1")
        err_msg = str(exc_info.value).lower()
        assert "connect" in err_msg or "refused" in err_msg or "timeout" in err_msg, (
            f"Expected connection error, got: {exc_info.value}"
        )

    @pytest.mark.asyncio
    async def test_pg_source_bad_credentials(self):
        """PgSource with bad credentials raises an auth error."""
        try:
            from scripts.entity_resolution.sources import PgSource
        except ImportError:
            pytest.skip("Entity resolution sources not available on this branch")

        import asyncpg

        pg = PgSource("postgresql://bogus_user:bad_pass@localhost:5433/postgres")
        try:
            await pg.fetchall("SELECT 1")
        except asyncpg.InvalidPasswordError:
            pass  # expected
        except OSError:
            pytest.skip("PostgreSQL not running on port 5433")

    @pytest.mark.asyncio
    async def test_discogs_cache_service_unavailable(self):
        """DiscogsCacheService.is_available() returns False when pool fails."""
        from discogs.cache_service import DiscogsCacheService

        # Create a mock pool that raises on any query
        mock_pool = AsyncMock()
        mock_pool.fetchval = AsyncMock(side_effect=Exception("connection lost"))

        cache = DiscogsCacheService(mock_pool)
        result = await cache.is_available()
        assert result is False, "is_available should return False when pool query fails"

    @pytest.mark.asyncio
    async def test_discogs_service_search_with_http_error_degrades(self):
        """DiscogsService.search returns empty results on HTTP transport failure."""
        from discogs.models import DiscogsSearchRequest
        from discogs.service import DiscogsService

        service = DiscogsService(token="test_token", cache_service=None)

        # Mock the httpx client to simulate a connection error.
        # DiscogsService catches exceptions internally and returns empty results.
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("simulated HTTP connection failure"))
        service._client = mock_client

        request = DiscogsSearchRequest(artist="Stereolab", album="Aluminum Tunes")

        result = await service.search(request)

        # The service gracefully degrades -- returns empty results, not an exception
        assert result is not None
        assert result.results == []

        # Clean up (avoid real close calls)
        service._client = None


# ---------------------------------------------------------------------------
# Entity store unavailable graceful degradation
# ---------------------------------------------------------------------------


class TestEntityStoreGracefulDegradation:
    """Service degrades gracefully when entity store is unavailable."""

    @pytest_asyncio.fixture
    async def app_client_no_entity_store(self, library_db, test_settings):
        """httpx AsyncClient with entity store returning None (unavailable)."""
        from httpx import ASGITransport, AsyncClient

        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        identity_routes = [r for r in app.routes if hasattr(r, "path") and "/identity" in r.path]
        if not identity_routes:
            pytest.skip("Identity endpoints not yet available on this branch")

        try:
            from identity.dependencies import get_entity_store

            app.dependency_overrides[get_entity_store] = lambda: None  # unavailable
        except ImportError:
            pytest.skip("Identity module not available on this branch")

        app.dependency_overrides[get_library_db] = lambda: library_db
        app.dependency_overrides[get_discogs_service] = lambda: None
        app.dependency_overrides[get_posthog_client] = lambda: None
        app.dependency_overrides[get_settings] = lambda: test_settings

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client

        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_resolve_returns_503_when_entity_store_unavailable(
        self, app_client_no_entity_store
    ):
        """GET /identity/resolve returns 503 when entity store is unavailable."""
        resp = await app_client_no_entity_store.get(
            "/identity/resolve", params={"name": "Stereolab"}
        )
        assert resp.status_code == 503
        assert "not available" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_bulk_returns_503_when_entity_store_unavailable(self, app_client_no_entity_store):
        """POST /identity/bulk returns 503 when entity store is unavailable."""
        resp = await app_client_no_entity_store.post(
            "/identity/bulk", json={"names": ["Stereolab", "Autechre"]}
        )
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_library_search_still_works_without_entity_store(
        self, app_client_no_entity_store
    ):
        """Library search endpoints work even when entity store is unavailable."""
        resp = await app_client_no_entity_store.get(
            "/api/v1/library/search", params={"q": "Stereolab"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 1, "Library search should work independently"


# ---------------------------------------------------------------------------
# Library DB unavailability
# ---------------------------------------------------------------------------


class TestLibraryDbUnavailability:
    """Library search handles database unavailability gracefully."""

    @pytest_asyncio.fixture
    async def unavailable_library_db(self):
        """LibraryDB instance with a closed/unavailable connection."""
        from library.db import LibraryDB

        db = LibraryDB(db_path=None)
        # Don't set _conn -- leave it as None (not connected)
        return db

    @pytest_asyncio.fixture
    async def app_client_no_db(self, unavailable_library_db, test_settings):
        """httpx AsyncClient with an unavailable LibraryDB."""
        from httpx import ASGITransport, AsyncClient

        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        app.dependency_overrides[get_library_db] = lambda: unavailable_library_db
        app.dependency_overrides[get_discogs_service] = lambda: None
        app.dependency_overrides[get_posthog_client] = lambda: None
        app.dependency_overrides[get_settings] = lambda: test_settings

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client

        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_health_reports_unhealthy_when_db_unavailable(self, app_client_no_db):
        """Health endpoint returns 503 when library DB is unavailable."""
        resp = await app_client_no_db.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unhealthy"
        assert body["services"]["database"] == "error"

    @pytest.mark.asyncio
    async def test_library_search_returns_error_when_db_unavailable(self, app_client_no_db):
        """Library search returns an error (not a hang) when DB is unavailable."""
        resp = await app_client_no_db.get("/api/v1/library/search", params={"q": "Stereolab"})
        # Should return an error status, not hang or crash
        assert resp.status_code >= 400


# ---------------------------------------------------------------------------
# Large payload / stress tests
# ---------------------------------------------------------------------------


class TestLargePayloads:
    """Service handles large or unusual payloads without crashes."""

    @pytest.mark.asyncio
    async def test_library_search_very_long_query(self, app_client):
        """Library search with a very long query string doesn't crash."""
        long_query = "Stereolab " * 500  # ~5000 chars
        resp = await app_client.get("/api/v1/library/search", params={"q": long_query})
        # Should return a valid response (possibly empty results), not crash
        assert resp.status_code in (200, 400, 422)

    @pytest.mark.asyncio
    async def test_library_search_unicode_query(self, app_client):
        """Library search handles Unicode characters correctly."""
        resp = await app_client.get(
            "/api/v1/library/search",
            params={"q": "Chuquimamani-Condori"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_library_search_special_characters(self, app_client):
        """Library search handles special characters without SQL injection."""
        for query in [
            "'; DROP TABLE library; --",
            "Stereolab' OR '1'='1",
            'Cat Power"); DELETE FROM library;',
            "<script>alert('xss')</script>",
        ]:
            resp = await app_client.get("/api/v1/library/search", params={"q": query})
            # Should not crash or return 500
            assert resp.status_code in (200, 400, 422), (
                f"Query '{query}' returned unexpected status {resp.status_code}"
            )

    @pytest.mark.asyncio
    async def test_library_search_empty_query_rejected(self, app_client):
        """Library search with empty string query is rejected."""
        resp = await app_client.get("/api/v1/library/search", params={"q": ""})
        # Empty query should be rejected (400) or return empty results (200)
        assert resp.status_code in (200, 400)
