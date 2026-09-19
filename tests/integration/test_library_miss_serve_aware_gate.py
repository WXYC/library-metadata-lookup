"""Integration test for the serve-aware step-3a gate (LML#1319) against real PG.

The issue's end-to-end shape: a songless lookup for (Eliana Glass, E at Home)
where the library holds only the sibling album "E" — a wrong-album
artist-fallback row that passes ``_filter_results_by_album_match``'s
``token_set_ratio`` floor (100 for any token subset) yet fails the LML#477
serve floor (``score_match`` = 20). Pre-fix, that row closed step 3a's
emptiness gate, so the local release cache — which holds the typed pair at
trigram score 1.0 — was never asked, and the row collapsed to the
``release_id=0`` streaming-only sentinel: real streaming URL, no Discogs id,
no artwork.

This test drives the REAL layers the unit tier mocks: ``perform_lookup`` →
the serve-aware gate → ``DiscogsService.search()``'s fallthrough seam →
``DiscogsCacheService.search_releases``'s trigram SQL against a seeded
``release`` / ``release_artist``. The live Discogs ``/database/search`` arm is
stubbed to return empty — the issue's regression criterion: a release present
in the local cache but absent from live Discogs search results (search-index
lag for fresh submissions) must still be matched by a songless row-less
lookup. The seed includes the duplicate-credit trap from the issue (extra=0
credit row + extra=1 role row for the same (release, artist)): prod carries
both for Eliana Glass, and ``search_releases``'s ``DISTINCT ON`` must absorb
them.

Run with: ``pytest -m pg -v tests/integration/test_library_miss_serve_aware_gate.py``
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService
from discogs.memory_cache import clear_all_caches
from discogs.service import DiscogsService
from lookup.models import LookupRequest
from tests.conftest import make_lml_telemetry
from tests.factories import make_library_item
from tests.integration.conftest import (
    F_UNACCENT_WRAPPER_SQL,
    skip_if_drop_targets_populated,
)

pytestmark = pytest.mark.pg

_E_AT_HOME_RELEASE_IDS = (37161147, 37901511)


class _EmptySearchResponse:
    """Live ``/database/search`` stub: HTTP 200 with zero results.

    Models the issue's trigger condition — Discogs's search index does not yet
    list a release the cache already holds by ID — as a genuine empty answer,
    not an outage.
    """

    status_code = 200

    def raise_for_status(self) -> None:  # pragma: no cover - trivial
        return None

    def json(self) -> dict:
        return {"results": [], "pagination": {"items": 0}}


@pytest_asyncio.fixture
async def seeded_cache_service(pg_pool):
    """Fresh ``release`` + ``release_artist`` seeded with the issue's rows."""
    async with pg_pool.acquire() as conn:
        await skip_if_drop_targets_populated(conn, ("release", "release_artist"))
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
        except Exception as e:
            pytest.skip(f"pg_trgm/unaccent extensions unavailable: {e}")
        await conn.execute(F_UNACCENT_WRAPPER_SQL)

        await conn.execute("DROP TABLE IF EXISTS release_artist CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")
        await conn.execute("""
            CREATE TABLE release (
                id          integer PRIMARY KEY,
                title       text NOT NULL,
                artwork_url text
            )
        """)
        await conn.execute("""
            CREATE TABLE release_artist (
                release_id  integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                artist_id   integer,
                artist_name text NOT NULL,
                extra       integer DEFAULT 0,
                role        text
            )
        """)

        await conn.executemany(
            "INSERT INTO release (id, title, artwork_url) VALUES ($1, $2, $3)",
            [
                (37161147, "E At Home", "https://img.discogs.com/e-at-home-lp.jpg"),
                (37901511, "E At Home", "https://img.discogs.com/e-at-home-ep.jpg"),
            ],
        )
        await conn.executemany(
            "INSERT INTO release_artist (release_id, artist_id, artist_name, extra, role)"
            " VALUES ($1, $2, $3, $4, $5)",
            [
                # The issue's dedup trap: the same (release, artist) carries a
                # plain credit row AND an extra=1 role row in prod.
                (37161147, 9001, "Eliana Glass", 0, None),
                (37161147, 9001, "Eliana Glass", 1, "Piano, Vocals"),
                (37901511, 9001, "Eliana Glass", 0, None),
                (37901511, 9001, "Eliana Glass", 1, "Piano, Vocals"),
            ],
        )

    # The @async_cached SEARCH_CACHE on DiscogsService.search is process-global
    # and keyed on the request, not the service instance — clear it on both
    # sides so no test here replays another's memoized response (and none
    # leaks one to a later suite).
    clear_all_caches()
    yield DiscogsCacheService(pg_pool)
    clear_all_caches()

    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS release_artist CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")


@pytest_asyncio.fixture
async def discogs_service(seeded_cache_service):
    """Real ``DiscogsService`` over the seeded cache; live search arm empty.

    ``get_release`` is stubbed to ``None`` (a normal "no extra metadata"
    outcome for enrichment) so the test pins the search probe's cache leg,
    not the release-detail schema.
    """
    service = DiscogsService(
        token="test-token-live-arm-stubbed", cache_service=seeded_cache_service
    )
    service._request_with_retry = AsyncMock(return_value=_EmptySearchResponse())
    service.get_release = AsyncMock(return_value=None)
    return service


@pytest.fixture
def fallback_library_db():
    """Library stub in the diagnosis replay's shape: the artist+album query
    misses, the artist-only fallback surfaces the sibling album "E"."""
    wrong_album_row = make_library_item(id=63861, artist="Eliana Glass", title="E")

    async def _search(query: str, limit: int = 10, **kwargs):
        if query.strip().lower() == "eliana glass":
            return [wrong_album_row]
        return []

    db = AsyncMock()
    db.search = AsyncMock(side_effect=_search)
    db.exact_title = AsyncMock(return_value=[])
    db.find_similar_artist = AsyncMock(return_value=None)
    db.is_available = AsyncMock(return_value=True)
    return db


class TestServeAwareGateAgainstRealCache:
    @pytest.mark.asyncio
    async def test_cached_pair_resolves_despite_wrong_album_fallback_row(
        self, fallback_library_db, discogs_service
    ):
        """The full LML#1319 shape: probe opened, trigram SQL answers, the
        typed pair leads the response with a real Discogs id + artwork — with
        the live search arm returning empty throughout — and the artist's
        shelved row is still returned behind it, call number intact (LML#1319
        review finding 1)."""
        from lookup.orchestrator import perform_lookup

        request = LookupRequest(
            artist="Eliana Glass",
            album="E at Home",
            raw_message="Eliana Glass - E at Home",
        )
        response = await perform_lookup(
            request, fallback_library_db, discogs_service, make_lml_telemetry()
        )

        assert len(response.results) == 2, f"got {response.results}"
        item = response.results[0]
        assert item.library_item.id == 0, (
            "expected the row-less synthesized pair to lead, got library row "
            f"{item.library_item.id} ({item.library_item.title!r})"
        )
        assert item.library_item.call_number == "(external)"
        assert item.artwork is not None
        assert item.artwork.release_id in _E_AT_HOME_RELEASE_IDS
        assert item.artwork.artwork_url in (
            "https://img.discogs.com/e-at-home-lp.jpg",
            "https://img.discogs.com/e-at-home-ep.jpg",
        )
        shelved = response.results[1]
        assert shelved.library_item.id == 63861
        assert shelved.library_item.call_number == "Rock CD S 1/1", (
            "the shelved row must keep the call number a DJ pulls the record by"
        )
        assert response.song_not_found is False

    @pytest.mark.asyncio
    async def test_probe_resolution_spends_no_live_search_for_the_typed_pair(
        self, fallback_library_db, discogs_service
    ):
        """The probe's own resolution is fully cache-served: the pg leg is
        terminal at the fallthrough seam, so no live ``/database/search``
        carries the typed album.

        Deliberately scoped to the typed pair rather than asserting zero live
        traffic overall. Keeping the shelved rows (LML#1319 review finding 1)
        means step 4 runs its normal per-row artwork pass, whose queries carry
        the ROW's title ("E") — traffic this request already paid before this
        PR existed, since the row occupied ``library_results`` then too. What
        must stay true is that opening the probe adds none of its own.

        This is the cache-HIT path; the cache-MISS cost of this lane is bounded
        separately by the ``allow_api_escalation=False`` posture (unit-tested in
        ``TestServeBlockedProbeIsCacheOnly``).
        """
        from lookup.orchestrator import perform_lookup

        request = LookupRequest(
            artist="Eliana Glass",
            album="E at Home",
            raw_message="Eliana Glass - E at Home",
        )
        response = await perform_lookup(
            request, fallback_library_db, discogs_service, make_lml_telemetry()
        )

        assert response.results[0].artwork is not None
        assert response.results[0].artwork.release_id in _E_AT_HOME_RELEASE_IDS

        typed_album_calls = [
            call
            for call in discogs_service._request_with_retry.call_args_list
            if "e at home" in str(call.kwargs.get("params", "")).lower()
        ]
        assert typed_album_calls == [], (
            f"the probe escalated the typed pair to the live API: {typed_album_calls}"
        )
