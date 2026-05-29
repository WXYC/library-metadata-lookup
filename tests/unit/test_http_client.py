"""Unit tests for scripts/streaming_availability/http_client.py."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from clients.streaming.base import BaseStreamingClient


class TestBaseStreamingClient:
    def test_init_sets_attributes(self):
        client = BaseStreamingClient(rate_limit=(10, 5), semaphore_limit=3)
        assert client._semaphore._value == 3

    @pytest.mark.asyncio
    async def test_get_client_creates_lazily(self):
        client = BaseStreamingClient(rate_limit=(10, 5), semaphore_limit=3)
        http = await client._get_client()
        assert http is not None
        await client.close()

    @pytest.mark.asyncio
    async def test_get_client_returns_same_instance(self):
        client = BaseStreamingClient(rate_limit=(10, 5), semaphore_limit=3)
        http1 = await client._get_client()
        http2 = await client._get_client()
        assert http1 is http2
        await client.close()

    @pytest.mark.asyncio
    async def test_close_is_safe_when_no_client(self):
        client = BaseStreamingClient(rate_limit=(10, 5), semaphore_limit=3)
        await client.close()  # Should not raise


class TestBaseStreamingClientGetClientRace:
    """Latent FD-leak race regression for _get_client.

    Same shape as LML#241 (FD-leak from concurrent first-callers in lazy
    singletons); fixed centrally in LML#242 + propagated to this site via
    wxyc-fastapi v0.3.0's ``async_singleton`` (this issue, #284).

    Without a lock around the lazy init, N concurrent first-callers each
    pass the ``self._http is None`` check, each construct a fresh
    ``httpx.AsyncClient``, and only one survives as ``self._http`` —
    the others are orphaned with their connection pools (and FDs) still
    open. Each subclass instance must hold its own (getter, closer) pair
    so subclass clients (Bandcamp, Deezer, Spotify, Apple Music) stay
    isolated.
    """

    @pytest.mark.asyncio
    async def test_concurrent_first_callers_construct_one_client(self):
        """50 concurrent first-callers must see one factory invocation.

        Force a yield-point inside the factory so every caller passes
        the outer ``is None`` check before any one of them assigns. With
        ``async_singleton``'s lock the factory runs once; without it,
        each caller constructs a fresh client and orphans 49 of them.
        """
        invocations: list[int] = []
        release_event = asyncio.Event()

        class TrackingAsyncClient:
            async def aclose(self):
                return None

        class SlowFactoryClient(BaseStreamingClient):
            def __init__(self):
                super().__init__(rate_limit=(10, 5), semaphore_limit=3)

            async def _make_client(self):
                invocations.append(1)
                # Yield long enough that every concurrent caller reaches
                # the ``await factory()`` boundary before any one resolves.
                await release_event.wait()
                return TrackingAsyncClient()

        client = SlowFactoryClient()
        tasks = [asyncio.create_task(client._get_client()) for _ in range(50)]
        for _ in range(4):
            await asyncio.sleep(0)
        release_event.set()
        results = await asyncio.gather(*tasks)

        assert len(invocations) == 1, (
            f"_make_client was invoked {len(invocations)} times for a single "
            "BaseStreamingClient under concurrent first-callers. The lazy "
            "init must be guarded by an asyncio.Lock — use "
            "wxyc_fastapi.http.async_singleton. See LML#241/#242 for the "
            "FD-leak shape; this is the latent twin tracked in #284."
        )
        first = results[0]
        assert all(r is first for r in results)

    @pytest.mark.asyncio
    async def test_subclass_instances_have_isolated_clients(self):
        """Two subclass instances must each get their own client.

        The (getter, closer) pair must be per-instance, not class-level.
        Sharing a single singleton across Bandcamp/Deezer/Spotify/Apple
        Music subclass instances would break their independent rate
        limits and timeouts.
        """

        class SubA(BaseStreamingClient):
            def __init__(self):
                super().__init__(rate_limit=(1, 1), semaphore_limit=1)

        class SubB(BaseStreamingClient):
            def __init__(self):
                super().__init__(rate_limit=(2, 1), semaphore_limit=2)

        a, b = SubA(), SubB()
        client_a = await a._get_client()
        client_b = await b._get_client()
        assert client_a is not client_b
        await a.close()
        await b.close()

    @pytest.mark.asyncio
    async def test_close_then_get_client_rebuilds(self):
        """Closing then re-getting must produce a fresh client.

        Long-running pipelines may bounce a client across pauses; the
        helper must not hand back a torn-down client.
        """
        client = BaseStreamingClient(rate_limit=(10, 5), semaphore_limit=3)
        first = await client._get_client()
        await client.close()
        second = await client._get_client()
        assert first is not second
        await client.close()

    @pytest.mark.asyncio
    async def test_close_invokes_aclose(self):
        """Closing must call aclose() on the underlying httpx client."""
        client = BaseStreamingClient(rate_limit=(10, 5), semaphore_limit=3)

        sentinel = AsyncMock()
        sentinel.aclose = AsyncMock()
        with patch(
            "clients.streaming.base.httpx.AsyncClient",
            return_value=sentinel,
        ):
            await client._get_client()
            await client.close()
        sentinel.aclose.assert_awaited_once()
