"""Integration tests for ``POST /api/v1/cache/refresh-for-identities`` (LML#525).

Exercises the full router + dispatcher stack end-to-end:

- Bulk PG read of ``entity.release_identity`` via the real ``EntityStore``.
- AsyncMock ``DiscogsService`` injected via ``app.dependency_overrides`` so we
  drive the release / artist responses without hitting Discogs.
- Cap / Pydantic / disconnect / per-item-error behavior at the router level.

Run with: ``pytest -m pg -v tests/integration/test_cache_refresh_for_identities.py``

The PG fixture mirrors the LML#526 ``set_up_entity_schema`` shape — drop and
recreate the ``entity`` schema (both artist-side and release-side tables) per
test so we own a clean substrate without depending on the live discogs-cache.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from discogs.models import ArtistCredit, ArtistDetails, ReleaseMetadataResponse
from entity.sources import PgSource
from entity.store import EntityStore
from tests.integration.conftest import ENTITY_IDENTITY_DDL, skip_if_drop_targets_populated


@pytest_asyncio.fixture
async def pg_pool(pg_pool_large):
    """Alias to the conftest ``max_size=4`` pool.

    This module's downstream fixtures and tests request ``pg_pool`` by name and
    historically got a ``max_size=4`` pool; aliasing keeps that cap without
    re-typing every request.
    """
    yield pg_pool_large


@pytest_asyncio.fixture(autouse=True)
async def set_up_entity_schema(pg_pool):
    """Drop + recreate the entity schema (artist + release tables).

    SAFETY: refuses to run when anything this fixture would drop already
    holds rows — the default ``DATABASE_URL_TEST`` may point at a real
    discogs-cache whose ``entity.*`` took hours of rate-limited
    reconciliation to build. The guard sweeps the entity schema dynamically
    from ``pg_class`` (see ``skip_if_drop_targets_populated``); this fixture
    drops no public tables, so its public-table list is empty.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_drop_targets_populated(conn, ())

    # Creation inside the try: a mid-setup failure must still drop whatever
    # was created instead of stranding rows that veto the next run (same
    # posture as the artists-route siblings).
    try:
        async with pg_pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")
            await conn.execute("CREATE SCHEMA entity")
            await conn.execute(ENTITY_IDENTITY_DDL)
            await conn.execute("""
                CREATE TABLE entity.reconciliation_log (
                    id SERIAL PRIMARY KEY,
                    identity_id INTEGER NOT NULL REFERENCES entity.identity(id),
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    confidence REAL,
                    method TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE entity.release_identity (
                    id SERIAL PRIMARY KEY,
                    discogs_release_id INTEGER UNIQUE,
                    discogs_master_id INTEGER UNIQUE,
                    musicbrainz_release_id TEXT UNIQUE,
                    spotify_album_id TEXT UNIQUE,
                    apple_music_album_id TEXT UNIQUE,
                    bandcamp_album_url TEXT UNIQUE,
                    reconciliation_status TEXT NOT NULL DEFAULT 'unreconciled',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            await conn.execute("""
                CREATE TABLE entity.release_reconciliation_log (
                    id SERIAL PRIMARY KEY,
                    identity_id INTEGER NOT NULL REFERENCES entity.release_identity(id),
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    confidence REAL,
                    method TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
        yield
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")


@pytest_asyncio.fixture
async def discogs_mock():
    """An ``AsyncMock`` ``DiscogsService`` with sensible defaults.

    Per-test calls override ``get_release.return_value`` and
    ``get_artist_details.side_effect`` to script the dispatcher's view.
    """
    mock = AsyncMock()
    mock.cache_service = None
    mock.get_release = AsyncMock(return_value=None)
    mock.get_artist_details = AsyncMock(return_value=None)
    return mock


async def _mint_release(pg_source: PgSource, source: str, external_id: str) -> int:
    """Mint a release_identity row via the real EntityStore and return id."""
    store = EntityStore(pg_source)
    identity_id, _ = await store.mint_or_get_release_identity(
        source=source, external_id=external_id
    )
    return identity_id


def _release(
    release_id: int,
    artists: list[ArtistCredit] | None = None,
) -> ReleaseMetadataResponse:
    return ReleaseMetadataResponse(
        release_id=release_id,
        title="DOGA",
        artist="Juana Molina",
        release_url=f"https://www.discogs.com/release/{release_id}",
        artists=artists if artists is not None else [ArtistCredit(artist_id=42, name="X")],
    )


def _artist(artist_id: int) -> ArtistDetails:
    return ArtistDetails(artist_id=artist_id, name=f"Artist {artist_id}")


_ROUTE = "/api/v1/cache/refresh-for-identities"


@pytest.mark.pg
class TestCacheRefreshHappyPath:
    @pytest.mark.asyncio
    async def test_single_discogs_release_with_two_artists_warms_and_walks(
        self, pg_app_client_with_discogs, pg_source, discogs_mock
    ):
        identity_id = await _mint_release(pg_source, "discogs_release", "12345")
        rel = _release(
            12345,
            artists=[
                ArtistCredit(artist_id=42, name="Juana Molina"),
                ArtistCredit(artist_id=99, name="Sessa"),
            ],
        )
        discogs_mock.get_release = AsyncMock(return_value=rel)
        discogs_mock.get_artist_details = AsyncMock(side_effect=[_artist(42), _artist(99)])

        resp = await pg_app_client_with_discogs.post(_ROUTE, json={"identity_ids": [identity_id]})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["results"]) == 1
        item = body["results"][0]
        assert item["identity_id"] == identity_id
        assert item["status"] == "warmed"
        sources = item["sources"]
        assert sources["discogs_release"]["release_outcome"] == "success"
        artists = sources["discogs_release"]["artists"]
        assert {a["external_id"] for a in artists} == {"42", "99"}
        assert all(a["outcome"] == "success" for a in artists)

    @pytest.mark.asyncio
    async def test_unknown_identity_id_returns_not_found_no_500(
        self, pg_app_client_with_discogs, discogs_mock
    ):
        resp = await pg_app_client_with_discogs.post(_ROUTE, json={"identity_ids": [999_999]})

        assert resp.status_code == 200, resp.text
        item = resp.json()["results"][0]
        assert item["identity_id"] == 999_999
        assert item["status"] == "not_found"
        assert item["sources"] is None
        discogs_mock.get_release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tombstone_counts_as_success_with_no_artists(
        self, pg_app_client_with_discogs, pg_source, discogs_mock
    ):
        """Tombstone-as-success: get_release returns None (LML#510 boundary)
        — release-leg ran and the cache state is current. No artists to walk.
        """
        identity_id = await _mint_release(pg_source, "discogs_release", "12345")
        discogs_mock.get_release = AsyncMock(return_value=None)

        resp = await pg_app_client_with_discogs.post(_ROUTE, json={"identity_ids": [identity_id]})

        item = resp.json()["results"][0]
        assert item["status"] == "warmed"
        assert item["sources"]["discogs_release"]["release_outcome"] == "success"
        assert item["sources"]["discogs_release"]["artists"] == []
        discogs_mock.get_artist_details.assert_not_awaited()


@pytest.mark.pg
class TestCacheRefreshMultiSource:
    @pytest.mark.asyncio
    async def test_discogs_master_only_row_returns_not_implemented(
        self, pg_app_client_with_discogs, pg_source, discogs_mock
    ):
        identity_id = await _mint_release(pg_source, "discogs_master", "789")

        resp = await pg_app_client_with_discogs.post(_ROUTE, json={"identity_ids": [identity_id]})

        item = resp.json()["results"][0]
        assert item["status"] == "not_implemented"
        assert item["sources"]["discogs_master"]["release_outcome"] == "not_implemented"
        discogs_mock.get_release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dual_populated_release_and_master_warms_via_release_leg(
        self, pg_app_client_with_discogs, pg_source, discogs_mock
    ):
        identity_id = await _mint_release(pg_source, "discogs_release", "12345")
        # Co-populate the master column on the same row.
        await pg_source.execute(
            "UPDATE entity.release_identity SET discogs_master_id = $1 WHERE id = $2",
            789,
            identity_id,
        )
        rel = _release(12345, artists=[ArtistCredit(artist_id=42, name="X")])
        discogs_mock.get_release = AsyncMock(return_value=rel)
        discogs_mock.get_artist_details = AsyncMock(return_value=_artist(42))

        resp = await pg_app_client_with_discogs.post(_ROUTE, json={"identity_ids": [identity_id]})

        item = resp.json()["results"][0]
        assert item["status"] == "warmed"
        assert item["sources"]["discogs_release"]["release_outcome"] == "success"
        assert item["sources"]["discogs_master"]["release_outcome"] == "not_implemented"


@pytest.mark.pg
class TestCacheRefreshSentinelGuard:
    """LML#525-specific contribution to the LML#518 / LML#546 audit posture."""

    @pytest.mark.asyncio
    async def test_walk_to_artist_skips_artist_id_zero_va_compilation(
        self, pg_app_client_with_discogs, pg_source, discogs_mock
    ):
        """A VA compilation with `artist_id=0` must NOT call get_artist_details(0)."""
        identity_id = await _mint_release(pg_source, "discogs_release", "12345")
        rel = _release(
            12345,
            artists=[
                ArtistCredit(artist_id=0, name="Various Artists"),
                ArtistCredit(artist_id=42, name="Juana Molina"),
            ],
        )
        discogs_mock.get_release = AsyncMock(return_value=rel)
        discogs_mock.get_artist_details = AsyncMock(return_value=_artist(42))

        resp = await pg_app_client_with_discogs.post(_ROUTE, json={"identity_ids": [identity_id]})

        item = resp.json()["results"][0]
        assert item["status"] == "warmed"
        artist_ids = [a["external_id"] for a in item["sources"]["discogs_release"]["artists"]]
        assert artist_ids == ["42"]
        called_with = [c.args[0] for c in discogs_mock.get_artist_details.await_args_list]
        assert called_with == [42]
        assert 0 not in called_with


@pytest.mark.pg
class TestCacheRefreshIsolation:
    @pytest.mark.asyncio
    async def test_per_item_failure_does_not_poison_siblings(
        self, pg_app_client_with_discogs, pg_source, discogs_mock
    ):
        ok_id = await _mint_release(pg_source, "discogs_release", "100")
        bad_id = await _mint_release(pg_source, "discogs_release", "200")
        ok_rel = _release(100, artists=[ArtistCredit(artist_id=42, name="X")])

        async def get_release_side_effect(release_id: int):
            if release_id == 200:
                raise RuntimeError("simulated upstream failure")
            return ok_rel

        discogs_mock.get_release = AsyncMock(side_effect=get_release_side_effect)
        discogs_mock.get_artist_details = AsyncMock(return_value=_artist(42))

        resp = await pg_app_client_with_discogs.post(_ROUTE, json={"identity_ids": [ok_id, bad_id]})

        results = {r["identity_id"]: r for r in resp.json()["results"]}
        assert results[ok_id]["status"] == "warmed"
        assert results[bad_id]["status"] == "error"
        assert results[bad_id]["sources"]["discogs_release"]["release_outcome"] == "error"
        assert results[bad_id]["sources"]["discogs_release"]["message"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_duplicate_identity_ids_each_run_independently(
        self, pg_app_client_with_discogs, pg_source, discogs_mock
    ):
        identity_id = await _mint_release(pg_source, "discogs_release", "12345")
        rel = _release(12345, artists=[])
        discogs_mock.get_release = AsyncMock(return_value=rel)

        resp = await pg_app_client_with_discogs.post(
            _ROUTE, json={"identity_ids": [identity_id, identity_id]}
        )

        body = resp.json()
        assert len(body["results"]) == 2
        assert all(r["identity_id"] == identity_id for r in body["results"])
        assert all(r["status"] == "warmed" for r in body["results"])


@pytest.mark.pg
class TestCacheRefreshInputValidation:
    @pytest.mark.asyncio
    async def test_oversize_batch_returns_400(self, pg_app_client_with_discogs):
        ids = list(range(1, 52))  # 51 ids > 50 cap
        resp = await pg_app_client_with_discogs.post(_ROUTE, json={"identity_ids": ids})
        assert resp.status_code == 400
        # Shared bulk envelope (LML#767) emits a generic "{cap}-item cap"
        # message; the 400 status is the load-bearing contract, not the noun.
        assert "50-item cap" in resp.text

    @pytest.mark.asyncio
    async def test_empty_identity_ids_returns_422(self, pg_app_client_with_discogs):
        resp = await pg_app_client_with_discogs.post(_ROUTE, json={"identity_ids": []})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_malformed_body_returns_400(self, pg_app_client_with_discogs):
        resp = await pg_app_client_with_discogs.post(
            _ROUTE,
            content=b"{not json}",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_non_array_identity_ids_returns_422(self, pg_app_client_with_discogs):
        resp = await pg_app_client_with_discogs.post(_ROUTE, json={"identity_ids": "1,2,3"})
        assert resp.status_code == 422


@pytest.mark.pg
class TestCacheRefreshClientDisconnect:
    """Mirror ``/api/v1/lookup/bulk``'s client-disconnect contract."""

    @staticmethod
    def _disconnect_after(delay_s: float = 0.05):
        async def fake_sentinel(_request):
            await asyncio.sleep(delay_s)
            return

        return fake_sentinel

    @pytest.mark.asyncio
    async def test_client_disconnect_returns_499_and_cancels_in_flight(
        self, pg_app_client_with_discogs, pg_source, discogs_mock
    ):
        identity_id = await _mint_release(pg_source, "discogs_release", "12345")

        async def slow_release(_release_id: int):
            await asyncio.sleep(5)
            return _release(12345)

        discogs_mock.get_release = AsyncMock(side_effect=slow_release)

        with patch("cache.router.watch_disconnect", self._disconnect_after()):
            resp = await asyncio.wait_for(
                pg_app_client_with_discogs.post(_ROUTE, json={"identity_ids": [identity_id]}),
                timeout=3.0,
            )

        assert resp.status_code == 499
