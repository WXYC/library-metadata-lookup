"""Unit tests for scripts/streaming_availability/http_client.py."""

import pytest

from scripts.streaming_availability.http_client import BaseStreamingClient


class TestBaseStreamingClient:
    def test_init_sets_attributes(self):
        client = BaseStreamingClient(rate_limit=(10, 5), semaphore_limit=3)
        assert client._http is None
        assert client._semaphore._value == 3

    @pytest.mark.asyncio
    async def test_get_client_creates_lazily(self):
        client = BaseStreamingClient(rate_limit=(10, 5), semaphore_limit=3)
        assert client._http is None
        http = await client._get_client()
        assert http is not None
        assert client._http is http
        await client.close()

    @pytest.mark.asyncio
    async def test_get_client_returns_same_instance(self):
        client = BaseStreamingClient(rate_limit=(10, 5), semaphore_limit=3)
        http1 = await client._get_client()
        http2 = await client._get_client()
        assert http1 is http2
        await client.close()

    @pytest.mark.asyncio
    async def test_close_clears_client(self):
        client = BaseStreamingClient(rate_limit=(10, 5), semaphore_limit=3)
        await client._get_client()
        assert client._http is not None
        await client.close()
        assert client._http is None

    @pytest.mark.asyncio
    async def test_close_is_safe_when_no_client(self):
        client = BaseStreamingClient(rate_limit=(10, 5), semaphore_limit=3)
        await client.close()  # Should not raise
        assert client._http is None
