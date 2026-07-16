"""Reproduce-or-refute for LML#817 (finding C2, #807 review) and its LML#825 follow-up.

#817 posits that the TRACK_ON_COMPILATION Wave-B V/A-compilation probe
(``DiscogsService.search_releases_by_track(..., artist_as_keyword=True)``) runs
through the #802 ``_SEARCH_BY_TRACK_ARTIST_SQL`` CTE and gets a V/A compilation
pruned by that CTE's ``rta.extra = 0`` guard *before* ``merge_wave_b_compilations``
sees it, when the queried performer's matching-track credit is ``extra = 1`` (a
guest/featured credit).

VERDICT: **REFUTED.** ``DiscogsService.search_releases_by_track`` sets
``pg_read_hook = None`` whenever ``artist_as_keyword=True`` (the guard predates
#802 — it landed in the fallthrough-seam refactor 3e95de8, not the #802 fix), so
the Wave-B probe never reads the PG cache CTE at all. It hits the Discogs API
keyword search (``params["q"] = artist`` + ``format=Compilation``). The
``rta.extra = 0`` guard the ticket names governs only the *artist-scoped* Wave-A
probe (``artist_as_keyword=False``), whose job is the non-compilation match; the
V/A comp reaches the caller via Wave B's API arm regardless of the ``extra`` flag
on any cached credit.

This module pins that split three ways so a future change that routes Wave B back
through the cache CTE (which would re-open the #817 mechanism) fails loudly:

- ``TestCacheCteExtraGuard`` — the ``rta.extra = 0`` prune is real *at the cache
  layer* (extra=1 dropped, extra=0 kept) but is only exercised by the artist-scoped
  read. This is the #333 precision guard; the #817 fear is real here, on Wave A.
- ``TestWaveBBypassesCacheCte`` — the controlled comparison that refutes the
  ticket's causal claim: with the API empty, a V/A comp is absent from the Wave-B
  result *whether its cached credit is extra=0 or extra=1*. If the drop were the
  ``rta.extra = 0`` prune, the extra=0 comp would surface (the CTE keeps it) and
  only extra=1 would drop. Both drop -> the cause is the cache being bypassed for
  Wave B, not the extra guard.
- ``TestWaveBApiArmRescue`` — the comp reaches the caller: with the Discogs API
  keyword search returning the comp, Wave B surfaces it despite an extra=1 cached
  credit, and the API was queried with ``q=<artist>`` + ``format=Compilation``.

LML#825 (follow-up, same mechanism): #817/#818 both noted a residual — in a
*cache-only degrade* (LML#755 saturation breaker OPEN, every live Discogs call
shed) Wave B's API arm returns empty, so a PG-cached extra=1 V/A comp is
unreachable through Wave B. The proposed fix was to give Wave B a degrade-time PG
read scoped to extra=1 V/A comps.

VERDICT: **REFUTED — the fix would recover nothing.** ``TestWaveBDegradeGapRefuted``
pins the two independent facts that rule it out:

1. Even if Wave B surfaced the PG-cached extra=1 comp during a degrade, it dies at
   the *validation* choke. ``DiscogsCacheService.validate_track_on_release`` reads
   ``release_track_artist WHERE extra = 0`` (the same #333 guard as the search
   CTE), so an extra=1-only credit is a definitive ``False`` — and ``False`` is a
   fallthrough *hit* (``_default_is_pg_hit(False)`` is ``True``), so
   ``DiscogsService.validate_track_on_release`` returns ``False`` **without ever
   reaching the (shed) API**. So a PG-cached extra=1 comp is dropped at validation
   whether the breaker is open or closed; the degrade adds no new loss for it. The
   only change that would let it through relaxes the extra=0 validation guard —
   exactly the #333/#719 precision trade-off #825 forbids relaxing (a writer /
   producer / featured credit is not a performance).
2. The extra=1 comps that *do* surface in normal operation are precisely those
   NOT in the PG cache: their validation misses the cache (``None`` → not cached)
   and falls to the API ``get_release``, which reads the real tracklist regardless
   of ``extra``. A degrade-time PG *read* cannot recover a comp that is not in PG.

So the current degrade behavior — drop the extra=1 comp — is the correct
conservative outcome, and a degrade-time PG read would only add L2 load in the
exact flood window (LML#706 discogs-cache buffer-thrash) for zero recovery.

Run with:
    pytest -m pg -v tests/integration/test_va_comp_wave_b_extra_credit.py
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from discogs.breaker import DiscogsBreakerOpenError
from discogs.cache_service import DiscogsCacheService
from discogs.memory_cache import clear_all_caches
from discogs.service import DiscogsService
from tests.integration.conftest import (
    F_UNACCENT_WRAPPER_SQL,
    skip_if_drop_targets_populated,
)

pytestmark = pytest.mark.pg

# Enough same-title decoys to fill the CTE's ``ORDER BY sim DESC LIMIT limit*2``
# ahead of the fuzzier-titled target -- the same flood mechanism the #802
# prefilter suite uses. > limit*2 (= 40 at limit=20).
N_DISTRACTORS = 42
COMMON_TITLE = "Fugitive Motel"
FUZZY_TITLE = "Fugitive Motel (Reprise)"  # sorts strictly below the exact-title decoys

# A representative V/A compilation shape: release artist "Various", performer
# credited on the matching track. WXYC-representative performer name.
COMP_ALBUM = "Nao Wave"
PERFORMER = "Juana Molina"


async def _create_schema(conn) -> None:
    """DROP/CREATE the four discogs-cache tables ``search_releases_by_track`` reads."""
    await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    await conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    await conn.execute(F_UNACCENT_WRAPPER_SQL)

    await conn.execute("DROP TABLE IF EXISTS release_track_artist CASCADE")
    await conn.execute("DROP TABLE IF EXISTS release_track CASCADE")
    await conn.execute("DROP TABLE IF EXISTS release_artist CASCADE")
    await conn.execute("DROP TABLE IF EXISTS release CASCADE")
    await conn.execute("""
        CREATE TABLE release (
            id    integer PRIMARY KEY,
            title text NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE release_track (
            release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
            sequence   integer NOT NULL,
            title      text NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE release_artist (
            release_id  integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
            artist_id   integer,
            artist_name text NOT NULL,
            extra       integer DEFAULT 0
        )
    """)
    await conn.execute("""
        CREATE TABLE release_track_artist (
            release_id     integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
            track_sequence integer NOT NULL,
            artist_name    text NOT NULL,
            extra          integer DEFAULT 0
        )
    """)


async def _seed_distractors(conn) -> None:
    """Seed ``N_DISTRACTORS`` exact-title decoy releases so the CTE ``LIMIT`` is full."""
    await conn.executemany(
        "INSERT INTO release (id, title) VALUES ($1, $2)",
        [(i, f"Decoy Album {i}") for i in range(1, N_DISTRACTORS + 1)],
    )
    await conn.executemany(
        "INSERT INTO release_track (release_id, sequence, title) VALUES ($1, 1, $2)",
        [(i, COMMON_TITLE) for i in range(1, N_DISTRACTORS + 1)],
    )
    await conn.executemany(
        "INSERT INTO release_artist (release_id, artist_name, extra) VALUES ($1, $2, 0)",
        [(i, f"Decoy Artist {i}") for i in range(1, N_DISTRACTORS + 1)],
    )


async def _seed_va_comp(conn, *, release_id: int, performer_extra: int) -> None:
    """Seed one V/A comp: release artist 'Various' (extra 0), performer on the track.

    The performer's ``release_track_artist`` credit carries ``performer_extra``
    (0 = main-artist track credit, 1 = guest/featured/writer credit). The
    release-level artist is 'Various', which never matches the performer, so
    surfacing this comp from the artist-scoped CTE depends entirely on the
    track-level ``rta`` leg -- exactly the axis #817 is about.
    """
    await conn.execute("INSERT INTO release (id, title) VALUES ($1, $2)", release_id, COMP_ALBUM)
    await conn.execute(
        "INSERT INTO release_track (release_id, sequence, title) VALUES ($1, 7, $2)",
        release_id,
        FUZZY_TITLE,
    )
    await conn.execute(
        "INSERT INTO release_artist (release_id, artist_name, extra) VALUES ($1, 'Various', 0)",
        release_id,
    )
    await conn.execute(
        "INSERT INTO release_track_artist "
        "(release_id, track_sequence, artist_name, extra) VALUES ($1, 7, $2, $3)",
        release_id,
        PERFORMER,
        performer_extra,
    )


@pytest_asyncio.fixture
async def cache(pg_pool):
    """Empty four-table discogs-cache schema; yields a service, drops on teardown."""
    async with pg_pool.acquire() as conn:
        await skip_if_drop_targets_populated(
            conn,
            ("release", "release_track", "release_artist", "release_track_artist"),
        )
        try:
            await _create_schema(conn)
        except Exception as e:
            pytest.skip(f"pg_trgm/unaccent extensions unavailable: {e}")
        # The full ``search_releases_by_track`` seam writes a durable negative on
        # an empty API result. All three Wave-B probes here share one negative key
        # -- ``(PERFORMER, COMMON_TITLE, artist_as_keyword=True)`` -- so an earlier
        # test's empty-API write would short-circuit a later same-key call via the
        # negative cache *before* its API arm, masking a real Wave-B routing
        # regression as a green run (or a spurious red). Own the "no negative
        # cache" precondition explicitly instead of relying on the table being
        # ambiently absent (it exists in the real discogs-cache schema, and
        # ``test_lookup_negative_cache`` creates it). This runs only past the
        # drop-target guard above, so a populated shared cache DB is never touched.
        await conn.execute("DROP TABLE IF EXISTS lookup_negative CASCADE")

    # The service method under test is wrapped in an L1 @async_cached(TRACK_CACHE);
    # clear it around each test so a prior test's (track, artist, artist_as_keyword)
    # result can't leak in as a memory-cache hit.
    clear_all_caches()
    yield DiscogsCacheService(pg_pool)
    clear_all_caches()

    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS release_track_artist CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release_track CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release_artist CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")
        await conn.execute("DROP TABLE IF EXISTS lookup_negative CASCADE")


def _make_service(cache: DiscogsCacheService, *, api_results: list[dict]) -> tuple:
    """Build a DiscogsService over the real cache with ``_request_with_retry`` stubbed.

    Returns ``(service, calls)`` where ``calls`` is a list the stub appends each
    call's ``params`` dict to, so a test can assert the API arm was hit with the
    expected keyword+format params. Every stubbed call returns the same
    ``{"results": api_results}`` payload; ``_process_search_result`` dedups by
    album so the supplemental keyword leg (fired when < 3 results) can't inflate.
    """
    service = DiscogsService(token="test-token", cache_service=cache)
    calls: list[dict] = []

    def _fake_response() -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock(return_value=None)
        resp.json = MagicMock(return_value={"results": api_results})
        return resp

    async def _stub(method, path, params=None, max_retries=None):
        calls.append(dict(params or {}))
        return _fake_response()

    service._request_with_retry = AsyncMock(side_effect=_stub)  # type: ignore[method-assign]
    return service, calls


def _shed_service(cache: DiscogsCacheService) -> DiscogsService:
    """A DiscogsService over the real cache whose every live Discogs call is shed.

    ``_request_with_retry`` raises :class:`DiscogsBreakerOpenError` — exactly what
    the LML#755 saturation breaker does when OPEN — so this faithfully simulates a
    cache-only degrade without driving real per-loop breaker state (the same
    stub-to-raise technique the row-less shed test uses). The ``AsyncMock`` also
    lets a test assert the API was never consulted (``assert_not_awaited``)."""
    service = DiscogsService(token="test-token", cache_service=cache)

    async def _shed(method, path, params=None, max_retries=None):
        raise DiscogsBreakerOpenError("shed")

    service._request_with_retry = AsyncMock(side_effect=_shed)  # type: ignore[method-assign]
    return service


class TestCacheCteExtraGuard:
    """The ``rta.extra = 0`` prune is real at the cache layer -- but only on the
    artist-scoped read the Wave-A probe uses. This is the #333 precision guard."""

    @pytest.mark.asyncio
    async def test_extra_one_track_credit_pruned_by_cte(self, cache):
        """Performer credited only via an ``extra = 1`` track credit -> the
        artist-scoped CTE drops the comp (``rta.extra = 0`` guard). This IS the
        mechanism #817 fears; it lives on Wave A, not Wave B."""
        async with cache.pool.acquire() as conn:
            await _seed_distractors(conn)
            await _seed_va_comp(conn, release_id=2001, performer_extra=1)

        results = await cache.search_releases_by_track(COMMON_TITLE, PERFORMER, 20)

        assert results == []

    @pytest.mark.asyncio
    async def test_extra_zero_track_credit_surfaced_by_cte(self, cache):
        """Same comp, but the performer's track credit is ``extra = 0`` -> the
        #802 ``rta`` leg keeps it despite the distractor flood. Establishes that
        the artist-scoped CTE *can* surface a V/A comp on a main-artist track
        credit, so the extra=1 drop above is the discriminator, not a blanket miss."""
        async with cache.pool.acquire() as conn:
            await _seed_distractors(conn)
            await _seed_va_comp(conn, release_id=2000, performer_extra=0)

        results = await cache.search_releases_by_track(COMMON_TITLE, PERFORMER, 20)

        assert [r.release_id for r in results] == [2000]
        assert results[0].album == COMP_ALBUM
        assert results[0].is_compilation is True


class TestWaveBBypassesCacheCte:
    """Controlled comparison refuting the #817 causal claim: with the API empty,
    a cached V/A comp is absent from the Wave-B result whether its credit is
    extra=0 or extra=1 -- the cache CTE (and its extra guard) is not consulted."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("performer_extra", [0, 1])
    async def test_comp_absent_when_api_empty_regardless_of_extra(self, cache, performer_extra):
        async with cache.pool.acquire() as conn:
            await _seed_distractors(conn)
            await _seed_va_comp(conn, release_id=3000, performer_extra=performer_extra)

        service, calls = _make_service(cache, api_results=[])

        response = await service.search_releases_by_track(
            COMMON_TITLE, PERFORMER, artist_as_keyword=True
        )

        # If Wave B read the cache CTE, the extra=0 comp (id 3000) would surface
        # (the CTE keeps it, per TestCacheCteExtraGuard). It does not -> the cache
        # is bypassed for Wave B; the extra flag is not the discriminator here.
        assert response.releases == []
        # And Wave B did hit the Discogs API keyword arm.
        assert calls, "Wave B must reach the API arm when the PG read is skipped"
        assert calls[0].get("q") == PERFORMER
        assert calls[0].get("format") == "Compilation"


class TestWaveBApiArmRescue:
    """The comp reaches the caller via Wave B's Discogs API keyword arm, so the
    extra=1 credit the ticket worries about does not silently drop it."""

    @pytest.mark.asyncio
    async def test_extra_one_comp_surfaces_via_api_arm(self, cache):
        async with cache.pool.acquire() as conn:
            await _seed_distractors(conn)
            await _seed_va_comp(conn, release_id=4001, performer_extra=1)

        # The Discogs keyword search returns the V/A comp (Discogs title shape
        # "Artist - Album"; "Various" -> is_compilation True downstream).
        service, calls = _make_service(
            cache, api_results=[{"id": 4001, "title": f"Various - {COMP_ALBUM}"}]
        )

        response = await service.search_releases_by_track(
            COMMON_TITLE, PERFORMER, artist_as_keyword=True
        )

        albums = {r.album for r in response.releases}
        assert COMP_ALBUM in albums, "the V/A comp must surface via the Wave-B API arm"
        surfaced = next(r for r in response.releases if r.album == COMP_ALBUM)
        assert surfaced.is_compilation is True
        # Proof it came from the API keyword arm, not the cache CTE.
        assert calls[0].get("q") == PERFORMER
        assert calls[0].get("format") == "Compilation"


class TestWaveBDegradeGapRefuted:
    """LML#825: during a cache-only degrade (LML#755 breaker OPEN, every live
    Discogs call shed) Wave B's API arm is empty, so a PG-cached extra=1 V/A comp
    is unreachable through Wave B. The proposed degrade-time Wave-B PG read is
    REFUTED here: the comp dies at validation regardless, and the surface-able
    extra=1 population is not in PG. See the module docstring for the full argument."""

    @pytest.mark.asyncio
    async def test_wave_b_empty_during_degrade(self, cache):
        """Ticket premise, reproduced: breaker OPEN -> Wave B
        (``artist_as_keyword=True``) returns empty even with the extra=1 V/A comp
        sitting in the PG cache (the API arm is shed; Wave B never reads PG)."""
        async with cache.pool.acquire() as conn:
            await _seed_distractors(conn)
            await _seed_va_comp(conn, release_id=5001, performer_extra=1)

        service = _shed_service(cache)

        response = await service.search_releases_by_track(
            COMMON_TITLE, PERFORMER, artist_as_keyword=True
        )

        assert response.releases == []
        # It really did try (and shed) the live API — the empty is a degrade, not
        # a PG read: Wave B has ``pg_read_hook=None``.
        service._request_with_retry.assert_awaited()

    @pytest.mark.asyncio
    async def test_extra_one_comp_fails_validation_from_pg_without_touching_api(self, cache):
        """The choke that refutes the fix. Even if a degrade-time Wave-B PG read
        surfaced the PG-cached extra=1 comp, ``validate_release_for_track`` drops
        it: ``DiscogsCacheService.validate_track_on_release`` reads
        ``release_track_artist WHERE extra = 0`` (the #333 guard), so the extra=1
        credit yields a definitive ``False`` — and ``False`` is a fallthrough hit
        (``_default_is_pg_hit(False)`` is ``True``), so
        ``DiscogsService.validate_track_on_release`` returns ``False`` WITHOUT ever
        reaching the (shed) API. Dropped identically whether the breaker is open or
        closed. The teeth: relaxing the extra=0 validation guard to close #825
        (the only change that makes the fix recover anything) flips this to
        ``True`` and fails here — reintroducing the #333/#719 imprecision."""
        async with cache.pool.acquire() as conn:
            await _seed_distractors(conn)
            await _seed_va_comp(conn, release_id=5002, performer_extra=1)

        service = _shed_service(cache)

        verdict = await service.validate_track_on_release(5002, FUZZY_TITLE, PERFORMER)

        assert verdict is False
        # It answered from PG and never consulted the shed API — else get_release
        # would have raised DiscogsBreakerOpenError through this call.
        service._request_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_extra_zero_comp_validates_from_pg_during_degrade(self, cache):
        """Discriminator + coverage of the extra=0 population Wave A already
        surfaces during a degrade: the same comp with an extra=0 track credit
        validates ``True`` from the PG cache under the identical shed, no API. So
        the ``extra`` axis is the discriminator (not a blanket degrade miss), and
        the extra=0 path — the one the ticket notes Wave A's ``rta`` leg already
        covers — validates fine cache-only."""
        async with cache.pool.acquire() as conn:
            await _seed_distractors(conn)
            await _seed_va_comp(conn, release_id=5003, performer_extra=0)

        service = _shed_service(cache)

        verdict = await service.validate_track_on_release(5003, FUZZY_TITLE, PERFORMER)

        assert verdict is True
        service._request_with_retry.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_surface_able_extra_one_comp_routes_validation_to_the_api(self, cache):
        """Why a degrade-time PG *read* cannot recover the surface-able population.
        An extra=1 comp that is NOT in the PG cache reads as ``None`` from
        ``DiscogsCacheService.validate_track_on_release`` (not cached) — a
        fallthrough miss. At the *service* layer that miss routes to the API
        ``get_release``, which reads the real tracklist regardless of ``extra``.
        That API route is exactly what the breaker sheds during a degrade, so the
        surface-able (non-PG) population is unreachable cache-only; a PG read
        cannot substitute for a comp that is not in PG.

        Pins both halves so the "routes to the API" claim in the name is actually
        exercised: (a) the cache genuinely can't answer for an uncached release
        (``None``), and (b) the *service* therefore drives the shed API —
        ``get_release`` re-raises ``DiscogsBreakerOpenError`` (the LML#755 FIX-1
        propagation) straight through ``validate_track_on_release``, and the shed
        seam is awaited. Contrast the load-bearing test above, where a PG-cached
        extra=1 comp answers ``False`` cache-only and never awaits the API."""
        # No comp seeded for id 5999: an uncached release, so PG can't answer.
        cache_verdict = await cache.validate_track_on_release(5999, FUZZY_TITLE, PERFORMER)
        assert cache_verdict is None

        # At the service layer the ``None`` cache miss falls through to the API
        # ``get_release`` -- which the breaker sheds during a degrade. This is the
        # route a PG read cannot replace for the non-PG surface-able population.
        service = _shed_service(cache)
        with pytest.raises(DiscogsBreakerOpenError):
            await service.validate_track_on_release(5999, FUZZY_TITLE, PERFORMER)
        service._request_with_retry.assert_awaited()
