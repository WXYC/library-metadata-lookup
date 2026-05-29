"""Integration tests for Discogs API endpoints."""

import os

import httpx
import pytest
import pytest_asyncio

from discogs.models import DiscogsSearchRequest
from discogs.service import DiscogsService
from entity.sources import SparqlSource
from scripts.entity_resolution.wikidata import WikidataReconciler

# Wikidata QIDs for entities used by ``TestEntityResolution``. QIDs are
# substantially more stable than Discogs IDs (Discogs admin operations can
# delete + re-create + re-assign IDs, see PR #204 for an incident); a QID is
# resolved to its current Discogs ID via SPARQL P1953 / P1954 / P2206 at
# fixture setup time so the tests survive Discogs reshuffling without a
# human re-pinning IDs.
#
# Releases (P2206) very rarely have their own Wikidata entries — albums are
# represented at the master level — so the release test stays pinned by
# integer ID with a comment.
_QID_STEREOLAB = "Q334652"
_QID_DOTS_AND_LOOPS_ALBUM = "Q3037397"
_PINNED_DOTS_AND_LOOPS_RELEASE_ID = 90416  # canonical vinyl LP, no Wikidata QID


# Track how many times the resolver is invoked so the module-scoped fixture
# can be tested for actually being module-scoped — not silently recreated by
# pytest-asyncio's loop-scope handling.
_resolver_call_count = 0


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def canonical_discogs_ids() -> dict[str, int]:
    """Resolve each test fixture QID to its current Discogs ID via Wikidata.

    Runs once per module (asserted via ``_resolver_call_count`` — see
    ``test_fixture_runs_once_per_module``). The result is a plain dict with
    no async resources held, so the value is safe to share across tests
    even though each test gets its own event loop.

    Skips dependent tests rather than passing a stale ID through on any of:
    - Wikidata SPARQL endpoint unreachable / timing out (httpx.HTTPError)
    - SPARQL response missing/malformed (asyncio.TimeoutError covers slow paths)
    - The QID exists but the property (P1953 / P1954) was removed
    - The Wikidata literal is non-numeric (defensive)
    """
    global _resolver_call_count
    _resolver_call_count += 1

    reconciler = WikidataReconciler(sparql=SparqlSource())
    try:
        artist_ids = await reconciler.resolve_discogs_ids_from_qids({_QID_STEREOLAB}, kind="artist")
        master_ids = await reconciler.resolve_discogs_ids_from_qids(
            {_QID_DOTS_AND_LOOPS_ALBUM}, kind="master"
        )
    # Narrow to network-shaped failures only — programmer errors (AttributeError
    # etc.) should still fail loud rather than be swallowed into a skip.
    except (httpx.HTTPError, TimeoutError) as exc:
        pytest.skip(f"Wikidata SPARQL unreachable ({type(exc).__name__}: {exc})")

    artist_id = artist_ids.get(_QID_STEREOLAB)
    master_id = master_ids.get(_QID_DOTS_AND_LOOPS_ALBUM)
    if artist_id is None or master_id is None:
        pytest.skip(
            "Wikidata SPARQL did not return Discogs IDs for the pinned QIDs "
            "(transient outage or property removed) — skipping ID-resolution tests."
        )
    return {
        "stereolab_artist": artist_id,
        "dots_and_loops_master": master_id,
    }


class TestDiscogsEndpoints:
    @pytest.mark.asyncio
    async def test_track_releases_503_without_service(self, app_client):
        resp = await app_client.get("/api/v1/discogs/track-releases", params={"track": "Song"})
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_release_503_without_service(self, app_client):
        resp = await app_client.get("/api/v1/discogs/release/123")
        assert resp.status_code == 503

    # /api/v1/discogs/search was removed in commit c5f1e65 (PR #157).

    @pytest.mark.asyncio
    async def test_artist_503_without_service(self, app_client):
        resp = await app_client.get("/api/v1/discogs/artist/45")
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_entity_503_without_service(self, app_client):
        resp = await app_client.get("/api/v1/discogs/entity/artist/45")
        assert resp.status_code == 503


@pytest.mark.external_api
class TestDiscogsApiSearch:
    """Tests that hit the real Discogs API (no cache) to verify search results."""

    @pytest.mark.asyncio
    async def test_high_rise_disallow_returns_correct_releases(self):
        """Discogs API should return 'Disallow' releases for High Rise, not unrelated albums.

        This is the regression test for the cache OR-vs-AND bug: the cache returned
        'Psychedelic Speed Freaks' (matching artist only), shadowing the correct results.
        """
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        service = DiscogsService(token=token, cache_service=None)
        try:
            response = await service.search(
                DiscogsSearchRequest(artist="High Rise", album="Disallow")
            )
        finally:
            await service.close()

        assert response.results, "Expected at least one result from Discogs API"
        top_result = response.results[0]
        assert "disallow" in top_result.album.lower(), (
            f"Top result should be 'Disallow', got '{top_result.album}'"
        )


@pytest.mark.external_api
class TestEntityResolution:
    """Integration tests for entity resolution using real Discogs API."""

    @pytest.mark.asyncio
    async def test_resolve_artist(self, canonical_discogs_ids):
        """Resolve Stereolab (Wikidata Q334652) and verify name + ID round-trip.

        The Discogs artist ID is resolved fresh from Wikidata P1953 at fixture
        setup, so the test survives Discogs admin operations that re-assign
        numeric IDs. See PR #204 for the incident this guard exists for.
        """
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        artist_id = canonical_discogs_ids["stereolab_artist"]
        service = DiscogsService(token=token, cache_service=None)
        try:
            result = await service.get_artist_details(artist_id)
        finally:
            await service.close()

        assert result is not None
        assert result.name == "Stereolab"
        assert result.artist_id == artist_id

    @pytest.mark.asyncio
    async def test_resolve_release(self):
        """Resolve a known release (Dots And Loops, vinyl LP, ID 90416) and verify title.

        Releases are not modeled at the entity level in Wikidata (only the
        abstract album / master is — see ``test_resolve_master``), so this
        test stays pinned to a numeric ID and will need manual re-pinning if
        Discogs ever reorganizes this specific pressing. The pinned ID is the
        canonical original Stereolab vinyl LP.
        """
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        service = DiscogsService(token=token, cache_service=None)
        try:
            result = await service.get_release(_PINNED_DOTS_AND_LOOPS_RELEASE_ID)
        finally:
            await service.close()

        assert result is not None
        assert result.title == "Dots And Loops"
        assert result.release_id == _PINNED_DOTS_AND_LOOPS_RELEASE_ID

    @pytest.mark.asyncio
    async def test_resolve_master(self, canonical_discogs_ids):
        """Resolve Dots And Loops master (Wikidata Q3037397) via P1954 and verify title."""
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        master_id = canonical_discogs_ids["dots_and_loops_master"]
        service = DiscogsService(token=token, cache_service=None)
        try:
            result = await service.get_master(master_id)
        finally:
            await service.close()

        assert result is not None
        assert result.title == "Dots And Loops"
        assert result.master_id == master_id

    @pytest.mark.asyncio
    async def test_fixture_runs_once_per_module(self, canonical_discogs_ids):
        """The module-scoped resolver fixture must not be silently recreated per test.

        pytest-asyncio's interaction between ``scope="module"`` fixtures and
        function-scoped event loops has historically caused fixtures to be
        re-evaluated per test — wasting Wikidata SPARQL round-trips and
        invalidating the docstring's "two SPARQL queries per session" claim.
        ``loop_scope="module"`` on the fixture pins this. By the time this
        test runs, the resolver should have been invoked exactly once across
        every test in the class that consumed the fixture.
        """
        # `canonical_discogs_ids` is requested here only so the fixture is
        # definitely live by the time we read the counter.
        assert canonical_discogs_ids is not None
        assert _resolver_call_count == 1, (
            f"canonical_discogs_ids fixture was invoked {_resolver_call_count} times; "
            "expected exactly 1 with module-scoped fixture + module-scoped loop."
        )

    @pytest.mark.asyncio
    async def test_artist_not_found(self):
        """Non-existent artist ID returns None."""
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        service = DiscogsService(token=token, cache_service=None)
        try:
            result = await service.get_artist_details(999999999)
        finally:
            await service.close()

        assert result is None

    @pytest.mark.asyncio
    async def test_release_not_found(self):
        """Non-existent release ID returns None."""
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        service = DiscogsService(token=token, cache_service=None)
        try:
            result = await service.get_release(999999999)
        finally:
            await service.close()

        assert result is None

    @pytest.mark.asyncio
    async def test_master_not_found(self):
        """Non-existent master ID returns None."""
        token = os.environ.get("DISCOGS_TOKEN")
        if not token:
            pytest.skip("DISCOGS_TOKEN not set")

        service = DiscogsService(token=token, cache_service=None)
        try:
            result = await service.get_master(999999999)
        finally:
            await service.close()

        assert result is None
