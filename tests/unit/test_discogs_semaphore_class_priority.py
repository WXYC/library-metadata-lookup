"""Unit tests for LML#927: class priority reaching the Discogs semaphore.

LML#953 already moved a class-5 ``/lookup`` request onto the low-priority
lane at the ENTRY gate (the LML#716/#924 process-global bulk permit,
``core.bulk_concurrency``). That gate stops at admission control -- it never
reaches the shared 5-permit Discogs concurrency semaphore itself
(``discogs/ratelimit.get_semaphore``), so a wave of class-5 requests that get
past it can still occupy every Discogs permit and head-of-line-block a
concurrent interactive request there.

This file pins the router-level wiring for the DEEPER reservation: each
handler sets a request-scoped low-priority ``ContextVar``
(``discogs.ratelimit.set_discogs_low_priority``) that ``DiscogsService.
_request_with_retry`` reads to decide whether to route through the new bulk
sub-semaphore before the shared one (see ``test_discogs_service_bulk_priority.py``
for the acquire-ordering pin in isolation). Here we drive it end to end
through the actual FastAPI handlers, mirroring
``test_lookup_class5_bulk_permit_ordering.py``'s shape (``_wait_until``,
occupier + ``Event``, ``httpx.ASGITransport``, patching
``lookup.router.perform_lookup``) but at this deeper gate: the fake
``perform_lookup`` below acquires the REAL primitives
(``get_bulk_discogs_semaphore`` / ``get_semaphore``) that
``_request_with_retry`` would, standing in for a live Discogs call.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import get_settings
from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
from discogs.ratelimit import (
    get_bulk_discogs_semaphore,
    get_semaphore,
    is_discogs_low_priority,
    reset_rate_limiting,
    set_discogs_low_priority,
)
from discogs.service import DiscogsService
from library.db import LibraryDB
from lookup.models import LookupResponse
from main import app
from tests.unit.conftest import override_deps

LOOKUP_BODY = {
    "artist": "Chuquimamani-Condori",
    "album": "Edits",
    "raw_message": "Chuquimamani-Condori - Edits",
}


async def _wait_until(predicate, *, timeout_s: float = 5.0, message: str = "condition"):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            pytest.fail(f"timed out after {timeout_s}s waiting for: {message}")
        await asyncio.sleep(0.005)


def _empty_response() -> LookupResponse:
    return LookupResponse(results=[], search_type="none")


@pytest.fixture(autouse=True)
def _reset_low_priority():
    set_discogs_low_priority(False)
    yield
    set_discogs_low_priority(False)


@pytest.fixture
def app_client(mock_settings):
    with override_deps(
        app,
        {
            get_library_db: AsyncMock(spec=LibraryDB),
            get_discogs_service: AsyncMock(spec=DiscogsService),
            get_posthog_client: None,
            get_settings: mock_settings,
        },
    ):
        yield app


class TestHandleLookupThreadsLowPriorityToDiscogsGate:
    """A class-5 ``/lookup`` sets the contextvar; classes 1-4/absent do not."""

    @pytest.mark.asyncio
    async def test_class_five_sets_low_priority_true(self, app_client):
        observed: list[bool] = []

        async def fake_lookup(request, **kwargs):
            observed.append(is_discogs_low_priority())
            return _empty_response()

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup", json=LOOKUP_BODY, headers={"X-Caller-Class": "5"}
                )

        assert resp.status_code == 200
        assert observed == [True]

    @pytest.mark.parametrize("caller_class", ["1", "2", "3", "4"])
    @pytest.mark.asyncio
    async def test_classes_one_through_four_leave_low_priority_false(
        self, app_client, caller_class
    ):
        observed: list[bool] = []

        async def fake_lookup(request, **kwargs):
            observed.append(is_discogs_low_priority())
            return _empty_response()

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup",
                    json=LOOKUP_BODY,
                    headers={"X-Caller-Class": caller_class},
                )

        assert resp.status_code == 200
        assert observed == [False]

    @pytest.mark.asyncio
    async def test_absent_header_leaves_low_priority_false(self, app_client):
        observed: list[bool] = []

        async def fake_lookup(request, **kwargs):
            observed.append(is_discogs_low_priority())
            return _empty_response()

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post("/api/v1/lookup", json=LOOKUP_BODY)

        assert resp.status_code == 200
        assert observed == [False]


class TestClassFiveDoesNotStarveClassOneAtTheDiscogsGate:
    """The core LML#927 regression, one level deeper than LML#953: class-5
    requests parked on the bulk Discogs sub-semaphore must not prevent a
    concurrent class-1 request from acquiring the shared 5-permit semaphore.
    """

    @pytest.mark.asyncio
    async def test_parked_class_five_does_not_block_class_one_discogs_acquire(
        self, app_client, monkeypatch
    ):
        monkeypatch.setenv("LML_DISCOGS_BULK_MAX_CONCURRENT", "1")
        reset_rate_limiting()

        release_bulk = asyncio.Event()

        async def fake_lookup(request, **kwargs):
            if is_discogs_low_priority():
                async with get_bulk_discogs_semaphore():
                    await release_bulk.wait()
            else:
                async with get_semaphore():
                    pass
            return _empty_response()

        with patch("lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                five_tasks = [
                    asyncio.create_task(
                        ac.post(
                            "/api/v1/lookup",
                            json=LOOKUP_BODY,
                            headers={"X-Caller-Class": "5"},
                        )
                    )
                    for _ in range(2)
                ]
                await _wait_until(
                    lambda: get_bulk_discogs_semaphore().locked(),
                    message="a class-5 request to occupy the bulk sub-semaphore",
                )

                one_resp = await asyncio.wait_for(
                    ac.post(
                        "/api/v1/lookup",
                        json=LOOKUP_BODY,
                        headers={"X-Caller-Class": "1"},
                    ),
                    timeout=1.0,
                )

                release_bulk.set()
                five_resps = await asyncio.gather(*five_tasks)

        assert one_resp.status_code == 200
        assert all(r.status_code == 200 for r in five_resps)


class TestHandleBulkLookupUnconditionallySetsLowPriority:
    @pytest.mark.asyncio
    async def test_bulk_lookup_item_sees_low_priority_true(self, mock_settings):
        observed: list[bool] = []

        async def fake_lookup(request, **kwargs):
            observed.append(is_discogs_low_priority())
            return _empty_response()

        with override_deps(
            app,
            {
                get_library_db: AsyncMock(spec=LibraryDB),
                get_discogs_service: AsyncMock(spec=DiscogsService),
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            with patch(
                "lookup.router.perform_lookup", new_callable=AsyncMock, side_effect=fake_lookup
            ):
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://test"
                ) as ac:
                    resp = await ac.post(
                        "/api/v1/lookup/bulk",
                        json={"items": [{"artist": "Sessa", "album": "Pequena Vertigem de Amor"}]},
                    )

        assert resp.status_code == 200
        assert observed == [True]


class TestIdentityBulkResolveLibrariesUnconditionallySetsLowPriority:
    @pytest.mark.asyncio
    async def test_bulk_resolve_libraries_sets_low_priority_true(self, mock_settings):
        from identity.dependencies import get_entity_store

        observed: list[bool] = []

        async def fake_resolve_library_name(artist_name: str):
            observed.append(is_discogs_low_priority())
            return None

        mock_entity_store = AsyncMock()
        mock_entity_store.resolve_library_name.side_effect = fake_resolve_library_name

        with override_deps(
            app,
            {
                get_settings: mock_settings,
                get_entity_store: mock_entity_store,
            },
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/v1/identity/bulk-resolve-libraries",
                    json={
                        "inputs": [
                            {
                                "library_id": 1,
                                "artist_name": "Large Professor",
                                "album_title": "1st Class",
                            }
                        ]
                    },
                )

        assert resp.status_code == 200
        assert observed == [True]


class TestCacheRefreshUnconditionallySetsLowPriority:
    @pytest.mark.asyncio
    async def test_refresh_for_identities_sets_low_priority_true(self, mock_settings):
        from identity.dependencies import get_entity_store

        observed: list[bool] = []

        async def fake_refresh(*, identity_id, **kwargs):
            observed.append(is_discogs_low_priority())
            from cache.models import CacheRefreshResultItem

            return CacheRefreshResultItem(identity_id=identity_id, status="not_found", sources=None)

        mock_entity_store = AsyncMock()
        mock_entity_store.get_release_identity_provenance_bulk.return_value = {
            42: [("discogs", "123")]
        }

        with (
            override_deps(
                app,
                {
                    get_settings: mock_settings,
                    get_entity_store: mock_entity_store,
                    get_discogs_service: AsyncMock(spec=DiscogsService),
                },
            ),
            patch(
                "cache.router.refresh_identity", new_callable=AsyncMock, side_effect=fake_refresh
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/v1/cache/refresh-for-identities", json={"identity_ids": [42]}
                )

        assert resp.status_code == 200
        assert observed == [True]
