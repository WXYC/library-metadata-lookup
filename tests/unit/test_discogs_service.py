"""Unit tests for discogs/service.py."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import discogs.service as discogs_service_module
from discogs.models import (
    DiscogsSearchRequest,
    DiscogsSearchResponse,
    ReleaseMetadataResponse,
    TrackItem,
    TrackReleasesResponse,
)
from discogs.service import DiscogsApiCheckResult, DiscogsService


def test_no_lockfree_lazy_init_in_get_client():
    """LML#435 (LML#357 audit follow-up): `DiscogsService._get_client` must
    not use the unguarded `self._client = httpx.AsyncClient(...)` lazy-init
    pattern.

    On cold start, two concurrent first-callers both see `self._client is
    None`, both construct a fresh `httpx.AsyncClient`, and only one is
    retained — the orphan is never closed (1 FD leaked per process lifetime
    per cold-start burst). Smaller magnitude than the #241 / #242 incident
    but the same class. Migrate to per-instance `async_singleton` (mirroring
    `clients/streaming/base.py:BaseStreamingClient`) so the race closes.
    """
    src = Path(discogs_service_module.__file__).read_text()
    assert "self._client = httpx.AsyncClient(" not in src, (
        "discogs/service.py constructs `self._client = httpx.AsyncClient(...)` "
        "inside a lock-free lazy-init — the racy pattern LML#357's audit "
        "flagged. Migrate to per-instance `async_singleton` (template: "
        "clients/streaming/base.py:BaseStreamingClient). See LML#435."
    )


@pytest.fixture
def service():
    svc = DiscogsService(token="test-token")
    return svc


@pytest.fixture
def service_with_cache(mock_asyncpg_pool):
    cache_svc = AsyncMock()
    svc = DiscogsService(token="test-token", cache_service=cache_svc)
    return svc


# ---------------------------------------------------------------------------
# Init / Client / Close
# ---------------------------------------------------------------------------


class TestDiscogsServiceInit:
    def test_init(self, service):
        assert service.token == "test-token"
        assert service.cache_service is None
        assert service._client is None

    def test_init_with_token_builds_token_auth_header(self):
        svc = DiscogsService(token="abc123")
        assert svc._auth_header == "Discogs token=abc123"

    def test_init_with_key_secret_builds_key_secret_auth_header(self):
        svc = DiscogsService(api_key="my-key", api_secret="my-secret")
        assert svc._auth_header == "Discogs key=my-key, secret=my-secret"

    def test_init_token_takes_precedence_over_key_secret(self):
        svc = DiscogsService(token="abc123", api_key="my-key", api_secret="my-secret")
        assert svc._auth_header == "Discogs token=abc123"

    def test_init_with_no_credentials_raises(self):
        with pytest.raises(ValueError, match="token or api_key"):
            DiscogsService()

    def test_init_with_partial_key_secret_raises(self):
        with pytest.raises(ValueError, match="token or api_key"):
            DiscogsService(api_key="only-key")

    @pytest.mark.asyncio
    async def test_get_client_uses_auth_header(self):
        svc = DiscogsService(api_key="my-key", api_secret="my-secret")
        client = await svc._get_client()
        assert client.headers["Authorization"] == "Discogs key=my-key, secret=my-secret"
        await svc.close()

    @pytest.mark.asyncio
    async def test_get_client_creates_once(self, service):
        client = await service._get_client()
        assert client is not None
        client2 = await service._get_client()
        assert client is client2
        await service.close()

    @pytest.mark.asyncio
    async def test_close(self, service):
        await service._get_client()
        await service.close()
        assert service._client is None

    @pytest.mark.asyncio
    async def test_close_without_client(self, service):
        await service.close()  # Should not raise


# ---------------------------------------------------------------------------
# check_api
# ---------------------------------------------------------------------------


class TestCheckApi:
    """check_api returns a DiscogsApiCheckResult discriminating failure modes."""

    @pytest.mark.asyncio
    async def test_check_api_200_returns_ok(self, service):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.get = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        assert await service.check_api() == DiscogsApiCheckResult.OK

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_check_api_auth_error(self, service, status):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_client.get = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        assert await service.check_api() == DiscogsApiCheckResult.AUTH_ERROR

    @pytest.mark.asyncio
    async def test_check_api_rate_limited(self, service):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_client.get = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        assert await service.check_api() == DiscogsApiCheckResult.RATE_LIMITED

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    async def test_check_api_upstream_error(self, service, status):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_client.get = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        assert await service.check_api() == DiscogsApiCheckResult.UPSTREAM_ERROR

    @pytest.mark.asyncio
    async def test_check_api_unknown_status_falls_through_to_error(self, service):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 418
        mock_client.get = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        assert await service.check_api() == DiscogsApiCheckResult.ERROR

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("dns fail"),
            httpx.ReadError("peer reset"),
            httpx.WriteError("write fail"),
            httpx.ConnectTimeout("connect timeout"),
            httpx.ReadTimeout("read timeout"),
            httpx.WriteTimeout("write timeout"),
            httpx.PoolTimeout("pool timeout"),
        ],
    )
    async def test_check_api_transport_errors_are_network_error(self, service, exc):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=exc)
        service._client = mock_client

        assert await service.check_api() == DiscogsApiCheckResult.NETWORK_ERROR

    @pytest.mark.asyncio
    async def test_check_api_unexpected_exception_is_error_and_logs(self, service, caplog):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=RuntimeError("boom"))
        service._client = mock_client

        import logging

        with caplog.at_level(logging.WARNING, logger="discogs.service"):
            assert await service.check_api() == DiscogsApiCheckResult.ERROR

        assert any("RuntimeError" in r.message or "boom" in r.message for r in caplog.records), (
            f"expected unexpected-exception log breadcrumb, got: {[r.message for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,expected",
        [
            (200, "ok"),
            (401, "auth-error"),
            (429, "rate-limited"),
            (503, "upstream-error"),
        ],
    )
    async def test_check_api_sets_sentry_tag_for_each_outcome(self, service, status, expected):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_client.get = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        with patch("discogs.service.sentry_sdk") as mock_sdk:
            await service.check_api()

        mock_sdk.set_tag.assert_called_once_with("discogs_api.check", expected)

    @pytest.mark.asyncio
    async def test_check_api_sets_sentry_tag_on_transport_error(self, service):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("fail"))
        service._client = mock_client

        with patch("discogs.service.sentry_sdk") as mock_sdk:
            await service.check_api()

        mock_sdk.set_tag.assert_called_once_with("discogs_api.check", "network-error")


# ---------------------------------------------------------------------------
# _request_with_retry
# ---------------------------------------------------------------------------


class TestRequestWithRetry:
    @pytest.mark.asyncio
    async def test_success(self, service):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_client.request = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        resp = await service._request_with_retry("GET", "/test", max_retries=0)
        assert resp is mock_resp

    @pytest.mark.asyncio
    async def test_429_retry(self, service):
        mock_client = AsyncMock()

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {}

        mock_client.request = AsyncMock(side_effect=[resp_429, resp_200])
        service._client = mock_client

        with patch("discogs.service.asyncio.sleep", new_callable=AsyncMock):
            resp = await service._request_with_retry("GET", "/test", max_retries=1)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_max_retries_exhausted(self, service):
        mock_client = AsyncMock()
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}
        mock_client.request = AsyncMock(return_value=resp_429)
        service._client = mock_client

        with patch("discogs.service.asyncio.sleep", new_callable=AsyncMock):
            resp = await service._request_with_retry("GET", "/test", max_retries=1)
        assert resp is None

    @pytest.mark.asyncio
    async def test_request_error(self, service):
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=httpx.RequestError("fail"))
        service._client = mock_client

        resp = await service._request_with_retry("GET", "/test", max_retries=0)
        assert resp is None

    @pytest.mark.asyncio
    async def test_request_error_log_includes_exception_type_and_request(self, service, caplog):
        """LIBRARY-METADATA-LOOKUP-7: when an httpx.RequestError with an empty
        message bubbles out of the request, the log line `Discogs request
        failed: {e}` produces `Discogs request failed:` with no diagnostic
        tail. The Sentry issue title is then unactionable. Capture the
        exception class name, the method/path being requested, and exc_info
        so the next outage is triage-able.
        """
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=httpx.ConnectError(""))
        service._client = mock_client

        with caplog.at_level("ERROR", logger="discogs.service"):
            resp = await service._request_with_retry("GET", "/database/search", max_retries=0)

        assert resp is None
        records = [r for r in caplog.records if "Discogs request failed" in r.getMessage()]
        assert len(records) == 1, "expected exactly one Discogs request failed log line"
        record = records[0]
        msg = record.getMessage()
        assert "ConnectError" in msg, f"log message missing exception class name: {msg!r}"
        assert "GET" in msg, f"log message missing HTTP method: {msg!r}"
        assert "/database/search" in msg, f"log message missing request path: {msg!r}"
        assert record.exc_info is not None, "log record should carry exc_info for Sentry traceback"

    @pytest.mark.asyncio
    async def test_429_honors_retry_after_header(self, service):
        """When Discogs sends Retry-After, sleep that long instead of exponential backoff."""
        mock_client = AsyncMock()
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "7"}
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {}
        mock_client.request = AsyncMock(side_effect=[resp_429, resp_200])
        service._client = mock_client

        with patch("discogs.service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            resp = await service._request_with_retry("GET", "/test", max_retries=1)
        assert resp.status_code == 200
        mock_sleep.assert_awaited_once_with(7.0)

    @pytest.mark.asyncio
    async def test_429_retry_after_capped_at_max_delay(self, service):
        """Retry-After values larger than the 60s cap are clamped."""
        mock_client = AsyncMock()
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "300"}
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {}
        mock_client.request = AsyncMock(side_effect=[resp_429, resp_200])
        service._client = mock_client

        with patch("discogs.service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await service._request_with_retry("GET", "/test", max_retries=1)
        delay = mock_sleep.await_args_list[0].args[0]
        assert delay == 60.0

    @pytest.mark.asyncio
    async def test_429_backoff_jittered(self, service):
        """Without Retry-After, backoff calls random.uniform(0.5, 1.5) for jitter."""
        mock_client = AsyncMock()
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}
        mock_client.request = AsyncMock(return_value=resp_429)
        service._client = mock_client

        with (
            patch("discogs.service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("discogs.service.random.uniform", return_value=1.25) as mock_uniform,
        ):
            await service._request_with_retry("GET", "/test", max_retries=3)
        # 3 retries → 3 sleep calls + 3 jitter calls
        assert mock_sleep.await_count == 3
        assert mock_uniform.call_count == 3
        # Each call to random.uniform should request the same jitter range.
        for call in mock_uniform.call_args_list:
            assert call.args == (0.5, 1.5)
        # Attempts 0, 1, 2 → bases 1, 2, 4 → with jitter factor 1.25 → 1.25, 2.5, 5.0
        delays = [c.args[0] for c in mock_sleep.await_args_list]
        assert delays == [1.25, 2.5, 5.0]

    @pytest.mark.asyncio
    async def test_429_invalid_retry_after_falls_back_to_backoff(self, service):
        """Non-numeric Retry-After (HTTP-date or junk) falls back to jittered backoff."""
        mock_client = AsyncMock()
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {}
        mock_client.request = AsyncMock(side_effect=[resp_429, resp_200])
        service._client = mock_client

        with patch("discogs.service.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            resp = await service._request_with_retry("GET", "/test", max_retries=1)
        assert resp.status_code == 200
        delay = mock_sleep.await_args_list[0].args[0]
        # Falls through to exponential at attempt 0: base 1s, jittered into [0.5, 1.5]
        assert 0.5 <= delay <= 1.5


# ---------------------------------------------------------------------------
# _request_with_retry: Sentry span instrumentation for acquire waits
# ---------------------------------------------------------------------------


class TestRequestWithRetrySpans:
    """Sentry-span coverage for the chokepoints that produce RCA dark time.

    The 85% un-instrumented dark time on `/api/v1/lookup` slow-path requests
    (24h prod data, Sentry trace explorer) is the queue wait *before* the 5-permit
    semaphore is acquired and the token-bucket wait inside the retry loop. These
    tests pin both `sentry_sdk.start_span` invocations so a future refactor can't
    silently drop the observability. See WXYC/library-metadata-lookup#358.
    """

    @pytest.mark.asyncio
    async def test_emits_semaphore_acquire_span(self, service):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_client.request = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        with patch("discogs.service.sentry_sdk") as mock_sdk:
            mock_sdk.start_span.return_value.__enter__.return_value = MagicMock()
            mock_sdk.start_span.return_value.__exit__.return_value = False
            await service._request_with_retry("GET", "/test", max_retries=0)

        span_names = [call.kwargs.get("name") for call in mock_sdk.start_span.call_args_list]
        assert "lml.discogs.semaphore" in span_names, (
            f"expected lml.discogs.semaphore span, got: {span_names}"
        )

    @pytest.mark.asyncio
    async def test_emits_rate_limiter_acquire_span(self, service):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_client.request = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        with patch("discogs.service.sentry_sdk") as mock_sdk:
            mock_sdk.start_span.return_value.__enter__.return_value = MagicMock()
            mock_sdk.start_span.return_value.__exit__.return_value = False
            await service._request_with_retry("GET", "/test", max_retries=0)

        span_names = [call.kwargs.get("name") for call in mock_sdk.start_span.call_args_list]
        assert "lml.discogs.rate_limiter" in span_names, (
            f"expected lml.discogs.rate_limiter span, got: {span_names}"
        )

    @pytest.mark.asyncio
    async def test_semaphore_span_carries_queue_depth(self, service):
        """The semaphore span must carry an `lml.semaphore.queue_depth` attribute.

        Sentry trace explorer uses this to surface backlog magnitude per call
        — distinguishing "first-in-line, paid full Discogs round-trip" from
        "queued behind N peers." Read approximate queue depth *before* the
        await so the value reflects pre-acquire load.
        """
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_client.request = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        captured_spans: dict[str, MagicMock] = {}

        def _start_span(*, op: str, name: str, **kwargs):
            ctx = MagicMock()
            span = MagicMock()
            ctx.__enter__.return_value = span
            ctx.__exit__.return_value = False
            captured_spans[name] = span
            return ctx

        with patch("discogs.service.sentry_sdk") as mock_sdk:
            mock_sdk.start_span.side_effect = _start_span
            await service._request_with_retry("GET", "/test", max_retries=0)

        sem_span = captured_spans.get("lml.discogs.semaphore")
        assert sem_span is not None, (
            f"expected a captured semaphore span, got names: {list(captured_spans)}"
        )
        data_keys = {call.args[0] for call in sem_span.set_data.call_args_list}
        assert "lml.semaphore.queue_depth" in data_keys, (
            f"expected lml.semaphore.queue_depth set_data, got: {data_keys}"
        )

    @pytest.mark.asyncio
    async def test_semaphore_released_after_exception(self, service):
        """The acquire/release pair must survive an exception inside the request.

        Rewriting `async with semaphore:` to explicit acquire/release is the
        instrumentation hook point, but it also takes ownership of the cleanup
        contract — a permit must not leak when the request body raises.
        """
        from discogs.ratelimit import get_semaphore, reset_rate_limiting

        reset_rate_limiting()

        mock_client = AsyncMock()
        # httpx.RequestError is caught inside _request_with_retry, so use a
        # non-handled exception class to force the try/finally to do the work.
        mock_client.request = AsyncMock(side_effect=RuntimeError("kaboom"))
        service._client = mock_client

        sem = get_semaphore()
        baseline = sem._value

        with pytest.raises(RuntimeError):
            await service._request_with_retry("GET", "/test", max_retries=0)

        assert sem._value == baseline, (
            f"semaphore leaked permits: baseline={baseline}, after={sem._value}"
        )

    @pytest.mark.asyncio
    async def test_semaphore_span_op_is_lock_acquire(self, service):
        """Both spans are tagged op='lock.acquire' so they roll up cleanly."""
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_client.request = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        with patch("discogs.service.sentry_sdk") as mock_sdk:
            mock_sdk.start_span.return_value.__enter__.return_value = MagicMock()
            mock_sdk.start_span.return_value.__exit__.return_value = False
            await service._request_with_retry("GET", "/test", max_retries=0)

        ops_by_name = {
            call.kwargs.get("name"): call.kwargs.get("op")
            for call in mock_sdk.start_span.call_args_list
        }
        assert ops_by_name.get("lml.discogs.semaphore") == "lock.acquire"
        assert ops_by_name.get("lml.discogs.rate_limiter") == "lock.acquire"


# ---------------------------------------------------------------------------
# _parse_title
# ---------------------------------------------------------------------------


class TestParseTitle:
    def test_artist_album(self):
        service = DiscogsService("t")
        assert service._parse_title("Queen - The Game") == ("Queen", "The Game")

    def test_no_separator(self):
        service = DiscogsService("t")
        assert service._parse_title("The Game") == ("", "The Game")


# ---------------------------------------------------------------------------
# _process_search_result
# ---------------------------------------------------------------------------


class TestProcessSearchResult:
    def test_valid_result(self, service):
        seen = set()
        result = service._process_search_result({"title": "Queen - The Game", "id": 123}, seen)
        assert result is not None
        assert result.album == "The Game"
        assert result.artist == "Queen"
        assert "the game" in seen

    def test_empty_title_returns_none(self, service):
        result = service._process_search_result({"title": "", "id": 1}, set())
        assert result is None

    def test_duplicate_skipped(self, service):
        seen = {"the game"}
        result = service._process_search_result({"title": "Queen - The Game", "id": 123}, seen)
        assert result is None

    def test_no_id_returns_none(self, service):
        result = service._process_search_result({"title": "Queen - The Game"}, set())
        assert result is None

    def test_compilation_detection(self, service):
        seen = set()
        result = service._process_search_result(
            {"title": "Various Artists - Compilation Album", "id": 1}, seen
        )
        assert result is not None
        assert result.is_compilation is True


# ---------------------------------------------------------------------------
# search_releases_by_track
# ---------------------------------------------------------------------------


class TestSearchReleasesByTrack:
    @pytest.mark.asyncio
    async def test_api_returns_results(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": [{"title": "Queen - The Game", "id": 123}]}

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.search_releases_by_track("Crazy Little Thing", "Queen")

        assert isinstance(result, TrackReleasesResponse)
        assert len(result.releases) >= 1

    @pytest.mark.asyncio
    async def test_cache_hit(self, service_with_cache):
        from discogs.models import ReleaseInfo

        service_with_cache.cache_service.search_releases_by_track = AsyncMock(
            return_value=[
                ReleaseInfo(
                    album="The Game",
                    artist="Queen",
                    release_id=123,
                    release_url="https://discogs.com/release/123",
                )
            ]
        )

        result = await service_with_cache.search_releases_by_track("Song", "Queen")
        assert result.cached is True
        assert len(result.releases) == 1

    @pytest.mark.asyncio
    async def test_cache_error_falls_back_to_api(self, service_with_cache):
        service_with_cache.cache_service.search_releases_by_track = AsyncMock(
            side_effect=Exception("cache down")
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": []}

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await service_with_cache.search_releases_by_track("Song", "Queen")
        assert isinstance(result, TrackReleasesResponse)

    @pytest.mark.asyncio
    async def test_supplement_search_when_few_results(self, service):
        """When fewer than 3 results, a supplementary keyword search runs."""
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.raise_for_status = MagicMock()
        resp1.json.return_value = {"results": [{"title": "Queen - Album1", "id": 1}]}

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.raise_for_status = MagicMock()
        resp2.json.return_value = {"results": [{"title": "Queen - Album2", "id": 2}]}

        with patch.object(
            service,
            "_request_with_retry",
            new_callable=AsyncMock,
            side_effect=[resp1, resp2],
        ):
            result = await service.search_releases_by_track("Song", "Queen")

        assert len(result.releases) == 2

    @pytest.mark.asyncio
    async def test_api_exception_returns_empty(self, service):
        with patch.object(
            service,
            "_request_with_retry",
            new_callable=AsyncMock,
            side_effect=Exception("API error"),
        ):
            result = await service.search_releases_by_track("Song")
        assert result.releases == []

    @pytest.mark.asyncio
    async def test_artist_as_keyword_skips_cache(self, service_with_cache):
        """When artist_as_keyword=True, the PG cache must be bypassed.

        The PG cache filters by release-level artist (release_artist table),
        which excludes VA compilations where the artist is credited on
        individual tracks. The artist_as_keyword flag tells the Discogs API
        to use keyword search + format=Compilation instead, so the cache
        must be skipped to let the API handle it.
        """
        from discogs.models import ReleaseInfo

        # Set up cache to return non-VA results (simulating the bug)
        service_with_cache.cache_service.search_releases_by_track = AsyncMock(
            return_value=[
                ReleaseInfo(
                    album="No Way Back",
                    artist="Adonis",
                    release_id=100,
                    release_url="https://discogs.com/release/100",
                )
            ]
        )
        # The negative cache is consulted in every path now (A4); pin its
        # return so this test still exercises the API leg.
        service_with_cache.cache_service.lookup_negative_hit = AsyncMock(return_value=False)
        service_with_cache.cache_service.record_lookup_negative = AsyncMock()

        # Set up API to return the VA compilation we actually want
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {
                    "title": "Various - Trax Records 20th Anniversary Collection",
                    "id": 456,
                }
            ]
        }

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await service_with_cache.search_releases_by_track(
                "No Way Back", "Adonis", artist_as_keyword=True
            )

        # Cache should NOT have been consulted
        service_with_cache.cache_service.search_releases_by_track.assert_not_called()
        # API result should come through
        assert len(result.releases) >= 1
        assert result.releases[0].is_compilation is True
        assert result.cached is False

    @pytest.mark.asyncio
    async def test_negative_cache_hit_short_circuits_api(self, service_with_cache):
        """A pre-existing negative-cache entry must skip the Discogs API entirely (LML#341 / A4)."""
        service_with_cache.cache_service.search_releases_by_track = AsyncMock(return_value=[])
        service_with_cache.cache_service.lookup_negative_hit = AsyncMock(return_value=True)
        service_with_cache.cache_service.record_lookup_negative = AsyncMock()

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
        ) as mock_request:
            result = await service_with_cache.search_releases_by_track(
                "Imaginary Track", "Imaginary Artist"
            )

        assert result.releases == []
        assert result.cached is True
        # The whole point: zero API calls fired.
        mock_request.assert_not_called()
        # Write-on-empty must NOT fire when the empty came from the cache.
        service_with_cache.cache_service.record_lookup_negative.assert_not_called()

    @pytest.mark.asyncio
    async def test_negative_cache_miss_falls_through_to_api(self, service_with_cache):
        """When the negative cache says no, the API is hit normally."""
        service_with_cache.cache_service.search_releases_by_track = AsyncMock(return_value=[])
        service_with_cache.cache_service.lookup_negative_hit = AsyncMock(return_value=False)
        service_with_cache.cache_service.record_lookup_negative = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [{"title": "Stereolab - Aluminum Tunes", "id": 1}]
        }

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await service_with_cache.search_releases_by_track("Fuses", "Stereolab")

        service_with_cache.cache_service.lookup_negative_hit.assert_called_once()
        assert len(result.releases) >= 1
        # Non-empty API response — negative-cache write must NOT fire.
        service_with_cache.cache_service.record_lookup_negative.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_api_response_writes_negative_cache(self, service_with_cache):
        """An API response with zero results must persist the negative verdict (LML#341 / A4)."""
        service_with_cache.cache_service.search_releases_by_track = AsyncMock(return_value=[])
        service_with_cache.cache_service.lookup_negative_hit = AsyncMock(return_value=False)
        service_with_cache.cache_service.record_lookup_negative = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": []}

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await service_with_cache.search_releases_by_track(
                "Definitely Not A Real Track", "Definitely Not A Real Artist"
            )

        assert result.releases == []
        service_with_cache.cache_service.record_lookup_negative.assert_called_once()

    @pytest.mark.asyncio
    async def test_api_exception_does_not_write_negative_cache(self, service_with_cache):
        """A 5xx or network failure is not a 'we asked, nothing' verdict — don't persist it."""
        service_with_cache.cache_service.search_releases_by_track = AsyncMock(return_value=[])
        service_with_cache.cache_service.lookup_negative_hit = AsyncMock(return_value=False)
        service_with_cache.cache_service.record_lookup_negative = AsyncMock()

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            side_effect=Exception("Discogs 502"),
        ):
            result = await service_with_cache.search_releases_by_track("X", "Y")

        assert result.releases == []
        service_with_cache.cache_service.record_lookup_negative.assert_not_called()

    @pytest.mark.asyncio
    async def test_negative_cache_consulted_for_artist_as_keyword(self, service_with_cache):
        """The negative cache also covers the keyword path — its key dimension distinguishes shapes."""
        service_with_cache.cache_service.search_releases_by_track = AsyncMock(return_value=[])
        service_with_cache.cache_service.lookup_negative_hit = AsyncMock(return_value=True)
        service_with_cache.cache_service.record_lookup_negative = AsyncMock()

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
        ) as mock_request:
            result = await service_with_cache.search_releases_by_track(
                "track", "artist", artist_as_keyword=True
            )

        # Positive-cache path was bypassed (artist_as_keyword=True), but the
        # negative-cache path was still consulted with artist_as_keyword=True
        # passed through.
        service_with_cache.cache_service.search_releases_by_track.assert_not_called()
        service_with_cache.cache_service.lookup_negative_hit.assert_called_once()
        call_args = service_with_cache.cache_service.lookup_negative_hit.call_args
        assert call_args.args[2] is True or call_args.kwargs.get("artist_as_keyword") is True
        # And the negative hit prevented any API call.
        assert result.releases == []
        assert result.cached is True
        mock_request.assert_not_called()


# ---------------------------------------------------------------------------
# get_release
# ---------------------------------------------------------------------------


class TestGetRelease:
    @pytest.mark.asyncio
    async def test_api_success(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "The Game",
            "artists": [{"name": "Queen"}],
            "year": 1980,
            "labels": [{"name": "EMI"}],
            "genres": ["Rock"],
            "styles": ["Arena Rock"],
            "tracklist": [
                {"position": "1", "title": "Play the Game", "duration": "3:30", "artists": []}
            ],
            "images": [{"uri": "https://img.com/cover.jpg"}],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(12345)

        assert result is not None
        assert result.title == "The Game"
        assert result.artist == "Queen"
        assert result.year == 1980
        assert result.artwork_url == "https://img.com/cover.jpg"
        assert len(result.tracklist) == 1

    @pytest.mark.asyncio
    async def test_cached_release(self, service_with_cache):
        cached = ReleaseMetadataResponse(
            release_id=123,
            title="Cached Album",
            artist="Artist",
            release_url="https://discogs.com/release/123",
            artwork_url="https://img.com/cached.jpg",
            # `artwork_checked_at` co-occurs with `artwork_url` in production
            # — `cache_service.write_release` stamps both in the same write,
            # and the bulk loader does too. Without this, the LML#542
            # widened predicate (`not_found OR tracklist OR
            # artwork_checked_at`) would mark this row as a miss because
            # `artwork_url` alone is not in the predicate.
            artwork_checked_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
            cached=True,
        )
        service_with_cache.cache_service.get_release = AsyncMock(return_value=cached)

        result = await service_with_cache.get_release(123)
        assert result.title == "Cached Album"
        assert result.cached is True

    @pytest.mark.asyncio
    async def test_cache_hit_with_null_artwork_and_null_checked_at_falls_through_to_api(
        self, service_with_cache
    ):
        """A cache row with `artwork_url IS NULL AND artwork_checked_at IS NULL`
        is the "never asked" state: the bulk loader populated the row but LML
        has not yet asked Discogs about the artwork. Predicate must fall through
        to the API to back-fill. Without this, bulk-loaded rows whose XML lacked
        images stay permanently artworkless and downstream consumers (BS V2, iOS)
        render placeholders.
        """
        stale = ReleaseMetadataResponse(
            release_id=33696615,
            title="Loved By Sound, Lost in Forms",
            artist="Lucy Liyou",
            release_url="https://discogs.com/release/33696615",
            artwork_url=None,
            artwork_checked_at=None,
            cached=True,
        )
        service_with_cache.cache_service.get_release = AsyncMock(return_value=stale)
        service_with_cache.cache_service.write_release = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Loved By Sound, Lost in Forms",
            "artists": [{"name": "Lucy Liyou"}],
            "tracklist": [],
            "images": [{"uri": "https://img.discogs.com/cover.jpg"}],
            "labels": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ) as mock_request:
            result = await service_with_cache.get_release(33696615)

        mock_request.assert_called_once()
        assert result is not None
        assert result.artwork_url == "https://img.discogs.com/cover.jpg"
        service_with_cache.cache_service.write_release.assert_called_once()
        written = service_with_cache.cache_service.write_release.call_args.args[0]
        assert written.artwork_url == "https://img.discogs.com/cover.jpg"

    @pytest.mark.asyncio
    async def test_cache_null_artwork_with_imageless_api_still_writes_back(
        self, service_with_cache
    ):
        """Cache row is "never asked" (artwork_url=None, artwork_checked_at=None)
        and the live API genuinely returns no images. Fall-through still
        completes: API result returned with artwork_url=None, write-back records
        the row. The next lookup must see artwork_checked_at set (from this
        write-back via cache_service.write_release) and treat it as a hit —
        covered by `test_cache_hit_with_checked_at_set_and_null_artwork_does_not_call_api`.
        """
        stale = ReleaseMetadataResponse(
            release_id=33696616,
            title="White Label Promo",
            artist="Stereolab",
            release_url="https://discogs.com/release/33696616",
            artwork_url=None,
            artwork_checked_at=None,
            cached=True,
        )
        service_with_cache.cache_service.get_release = AsyncMock(return_value=stale)
        service_with_cache.cache_service.write_release = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "White Label Promo",
            "artists": [{"name": "Stereolab"}],
            "tracklist": [],
            "images": [],
            "labels": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ) as mock_request:
            result = await service_with_cache.get_release(33696616)

        mock_request.assert_called_once()
        assert result is not None
        assert result.artwork_url is None
        service_with_cache.cache_service.write_release.assert_called_once()
        written = service_with_cache.cache_service.write_release.call_args.args[0]
        assert written.artwork_url is None

    @pytest.mark.asyncio
    async def test_cache_hit_with_checked_at_set_and_null_artwork_does_not_call_api(
        self, service_with_cache
    ):
        """Cache row has ``artwork_url=None`` but ``artwork_checked_at`` set —
        the "asked Discogs, genuinely no image" state. Predicate must treat
        this as a full hit and skip the API call. Without honoring
        ``artwork_checked_at``, LML re-fetches every imageless release on
        every lookup, burning Discogs rate limit (WXYC#423).
        """
        cached = ReleaseMetadataResponse(
            release_id=33696616,
            title="White Label Promo",
            artist="Stereolab",
            release_url="https://discogs.com/release/33696616",
            artwork_url=None,
            artwork_checked_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=UTC),
            cached=True,
        )
        service_with_cache.cache_service.get_release = AsyncMock(return_value=cached)
        service_with_cache.cache_service.write_release = AsyncMock()

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
        ) as mock_request:
            result = await service_with_cache.get_release(33696616)

        mock_request.assert_not_called()
        service_with_cache.cache_service.write_release.assert_not_called()
        assert result is cached

    @pytest.mark.asyncio
    async def test_cache_hit_with_tracklist_and_null_artwork_columns_does_not_call_api(
        self, service_with_cache
    ):
        """LML#542: a cache row with the full release tree populated
        (`release_track` rows exist, surfaced as a non-empty ``tracklist``) is
        a HIT regardless of whether ``artwork_url`` / ``artwork_checked_at``
        are NULL. The artwork-columns gate from #423 was costing ~20% of
        ``get_release`` calls a full Discogs round-trip even though every
        column except artwork was already in PG — diagnosed in #537's
        `cache_miss_provenance` probe. Widening the predicate to accept
        ``release_track``-populated rows recovers that miss rate; artwork
        backfill is decoupled.
        """
        cached = ReleaseMetadataResponse(
            release_id=33696617,
            title="Aluminum Tunes",
            artist="Stereolab",
            release_url="https://discogs.com/release/33696617",
            artwork_url=None,
            artwork_checked_at=None,
            tracklist=[
                TrackItem(position="A1", title="Pop Quiz", duration="3:33", artists=[]),
                TrackItem(position="A2", title="The Extension Trip", duration="4:12", artists=[]),
            ],
            cached=True,
        )
        service_with_cache.cache_service.get_release = AsyncMock(return_value=cached)
        service_with_cache.cache_service.write_release = AsyncMock()

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
        ) as mock_request:
            result = await service_with_cache.get_release(33696617)

        mock_request.assert_not_called()
        service_with_cache.cache_service.write_release.assert_not_called()
        assert result is cached
        # Artwork stays NULL on the cached response — the point of the
        # widening is to stop paying a Discogs round-trip purely to populate
        # artwork; the field remains nullable for consumers.
        assert result.artwork_url is None
        assert result.artwork_checked_at is None

    @pytest.mark.asyncio
    async def test_cache_hit_tombstone_only_signal_does_not_call_api(self, service_with_cache):
        """LML#542 tombstone wrinkle: ``not_found = True`` rows are HITs even
        if ``artwork_checked_at`` happens to be unset and the child cascade
        is empty. The tombstone write path in ``cache_service.write_release``
        stamps ``artwork_checked_at = now()`` today, so in practice the
        ``artwork_checked_at`` arm of the predicate already catches them —
        but the predicate must not silently regress to "tombstone with NULL
        ``artwork_checked_at`` falls through to the API" if a future code
        path ever produces one. The boundary translates the tombstone back
        to ``None`` for the caller (LML#510).
        """
        tombstone = ReleaseMetadataResponse(
            release_id=33696618,
            title="",
            artist="",
            release_url="https://www.discogs.com/release/33696618",
            artwork_url=None,
            artwork_checked_at=None,
            tracklist=[],
            not_found=True,
            cached=True,
        )
        service_with_cache.cache_service.get_release = AsyncMock(return_value=tombstone)
        service_with_cache.cache_service.write_release = AsyncMock()

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
        ) as mock_request:
            result = await service_with_cache.get_release(33696618)

        mock_request.assert_not_called()
        service_with_cache.cache_service.write_release.assert_not_called()
        # LML#510 boundary: tombstone → None.
        assert result is None

    @pytest.mark.asyncio
    async def test_404_returns_none(self, service):
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=None
        ):
            result = await service.get_release(99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_write_back_to_cache(self, service_with_cache):
        service_with_cache.cache_service.get_release = AsyncMock(return_value=None)
        service_with_cache.cache_service.write_release = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Album",
            "artists": [{"name": "Artist"}],
            "tracklist": [],
            "images": [],
            "labels": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            await service_with_cache.get_release(456)

        service_with_cache.cache_service.write_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_write_error_still_returns(self, service_with_cache):
        service_with_cache.cache_service.get_release = AsyncMock(return_value=None)
        service_with_cache.cache_service.write_release = AsyncMock(
            side_effect=Exception("write fail")
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Album",
            "artists": [{"name": "Artist"}],
            "tracklist": [],
            "images": [],
            "labels": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await service_with_cache.get_release(789)

        assert result is not None

    @pytest.mark.asyncio
    async def test_api_maps_videos(self, service):
        """Videos in API response are mapped to ReleaseVideo objects."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Emperor Tomato Ketchup",
            "artists": [{"name": "Stereolab"}],
            "year": 1996,
            "labels": [],
            "genres": ["Electronic"],
            "styles": ["Krautrock"],
            "tracklist": [],
            "images": [],
            "videos": [
                {
                    "uri": "https://www.youtube.com/watch?v=abc",
                    "title": "Metronomic Underground",
                    "duration": 456,
                    "embed": True,
                },
                {
                    "uri": "https://www.youtube.com/watch?v=def",
                    "title": "French Disko",
                    "duration": 204,
                    "embed": False,
                },
            ],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(12345)

        assert result is not None
        assert len(result.videos) == 2
        assert result.videos[0].src == "https://www.youtube.com/watch?v=abc"
        assert result.videos[0].title == "Metronomic Underground"
        assert result.videos[0].duration == 456
        assert result.videos[0].embed is True
        assert result.videos[1].embed is False

    @pytest.mark.asyncio
    async def test_empty_uri_videos_are_skipped(self, service):
        """Videos without a URI are not included in the result."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "On Your Own Love Again",
            "artists": [{"name": "Jessica Pratt"}],
            "year": 2015,
            "labels": [],
            "genres": [],
            "styles": [],
            "tracklist": [],
            "images": [],
            "videos": [
                {
                    "uri": "https://www.youtube.com/watch?v=abc",
                    "title": "Moon Dust",
                    "duration": 180,
                    "embed": True,
                },
                {"uri": "", "title": "No URI video", "duration": 100, "embed": True},
                {"title": "Missing URI key entirely", "duration": 100, "embed": True},
            ],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(99)

        assert result is not None
        assert len(result.videos) == 1
        assert result.videos[0].src == "https://www.youtube.com/watch?v=abc"

    @pytest.mark.asyncio
    async def test_no_videos_in_api_response(self, service):
        """Releases without videos return an empty videos list."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "DOGA",
            "artists": [{"name": "Juana Molina"}],
            "labels": [],
            "genres": [],
            "styles": [],
            "tracklist": [],
            "images": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(55)

        assert result is not None
        assert result.videos == []


# ---------------------------------------------------------------------------
# search_releases_by_album_title
# ---------------------------------------------------------------------------


class TestSearchReleasesByAlbumTitle:
    """Unit tests for the album-title fallback (#319).

    Asserts that the new method dispatches to the Discogs API with the
    expected params, parses results through the shared ``_process_search_result``
    pipeline, and gracefully handles edge cases (blank input, API error).
    """

    @pytest.mark.asyncio
    async def test_dispatches_with_release_title_and_no_format_filter(self, service):
        """No ``format=Compilation`` constraint — release 34993109 (the trio
        case from #237) is not classified as a compilation in Discogs, so
        filtering by format would exclude the motivating target."""
        captured: dict = {}

        async def fake_request(method, path, params=None, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["params"] = params
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "results": [
                    {"title": "Various - Some Comp", "id": 999},
                ]
            }
            return mock_resp

        with patch.object(service, "_request_with_retry", new=fake_request):
            result = await service.search_releases_by_album_title("Some Comp", limit=5)

        assert captured["method"] == "GET"
        assert captured["path"] == "/database/search"
        assert captured["params"]["release_title"] == "Some Comp"
        assert captured["params"]["type"] == "release"
        assert captured["params"]["per_page"] == 5
        assert "format" not in captured["params"]
        assert isinstance(result, TrackReleasesResponse)
        assert len(result.releases or []) == 1

    @pytest.mark.asyncio
    async def test_different_pressings_of_same_album_are_kept_distinct(self, service):
        """Discogs commonly returns multiple release IDs for the same album title
        (different pressings, regions, formats). The fallback must keep them all
        as candidates — the orchestrator's library + track validation picks the
        right one. The trio repro from #237 has five such releases titled
        ``Orcutt Shelley Miller``; if we dedupe by album, the target (34993109)
        is silently dropped because it sorts after another pressing."""

        async def fake_request(method, path, params=None, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {
                "results": [
                    {"title": "Same Artist - Same Album", "id": 1},
                    {"title": "Same Artist - Same Album", "id": 2},
                    {"title": "Same Artist - Same Album", "id": 3},
                ]
            }
            return mock_resp

        with patch.object(service, "_request_with_retry", new=fake_request):
            result = await service.search_releases_by_album_title("Same Album", limit=10)

        ids = sorted(r.release_id for r in (result.releases or []))
        assert ids == [1, 2, 3], f"Expected all three pressings to survive; got release_ids={ids}"

    @pytest.mark.asyncio
    async def test_returns_empty_for_blank_album(self, service):
        result = await service.search_releases_by_album_title("   ")
        assert isinstance(result, TrackReleasesResponse)
        assert result.releases == [] or not result.releases

    @pytest.mark.asyncio
    async def test_api_exception_returns_empty_response(self, service):
        with patch.object(
            service,
            "_request_with_retry",
            new_callable=AsyncMock,
            side_effect=Exception("network down"),
        ):
            result = await service.search_releases_by_album_title("Some Comp")
        assert isinstance(result, TrackReleasesResponse)
        assert result.releases == [] or not result.releases


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    @pytest.mark.asyncio
    async def test_api_success(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [{"title": "Queen - The Game", "id": 1, "thumb": "https://img.com/t.jpg"}]
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.search(DiscogsSearchRequest(artist="Queen", album="The Game"))

        assert isinstance(result, DiscogsSearchResponse)
        assert len(result.results) == 1

    @pytest.mark.asyncio
    async def test_fuzzy_fallback_on_empty(self, service):
        """When strict search returns empty, tries fuzzy query."""
        resp_empty = MagicMock()
        resp_empty.status_code = 200
        resp_empty.raise_for_status = MagicMock()
        resp_empty.json.return_value = {"results": []}

        resp_fuzzy = MagicMock()
        resp_fuzzy.status_code = 200
        resp_fuzzy.raise_for_status = MagicMock()
        resp_fuzzy.json.return_value = {
            "results": [{"title": "Queen - Game", "id": 2, "thumb": ""}]
        }

        with patch.object(
            service,
            "_request_with_retry",
            new_callable=AsyncMock,
            side_effect=[resp_empty, resp_fuzzy],
        ):
            result = await service.search(DiscogsSearchRequest(artist="Queen", album="Game"))

        assert len(result.results) >= 1

    @pytest.mark.asyncio
    async def test_no_search_fields_returns_empty(self, service):
        result = await service.search(DiscogsSearchRequest())
        assert result.results == []

    @pytest.mark.asyncio
    async def test_cache_hit(self, service_with_cache):
        service_with_cache.cache_service.search_releases = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "Artist",
                    "artwork_url": "https://img.com/a.jpg",
                }
            ]
        )

        result = await service_with_cache.search(DiscogsSearchRequest(artist="Artist"))
        assert result.cached is True
        assert len(result.results) == 1

    @pytest.mark.asyncio
    async def test_cache_error_falls_back_to_api(self, service_with_cache):
        service_with_cache.cache_service.search_releases = AsyncMock(
            side_effect=Exception("cache error")
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": []}

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await service_with_cache.search(DiscogsSearchRequest(artist="Artist"))
        assert isinstance(result, DiscogsSearchResponse)

    @pytest.mark.asyncio
    async def test_spacer_gif_filtered(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [{"title": "Art - Alb", "id": 1, "thumb": "https://img.com/spacer.gif"}]
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.search(DiscogsSearchRequest(artist="Art"))
        assert result.results[0].artwork_url is None


# ---------------------------------------------------------------------------
# _build_search_params
# ---------------------------------------------------------------------------


class TestBuildSearchParams:
    def test_artist_and_album(self, service):
        params = service._build_search_params(
            DiscogsSearchRequest(artist="Queen", album="The Game")
        )
        assert params["artist"] == "Queen"
        assert params["release_title"] == "The Game"

    def test_artist_and_track(self, service):
        params = service._build_search_params(DiscogsSearchRequest(artist="Queen", track="Song"))
        assert params["release_title"] == "Song"

    def test_no_fields_returns_empty(self, service):
        params = service._build_search_params(DiscogsSearchRequest())
        assert params == {}

    def test_includes_label_when_provided(self, service):
        params = service._build_search_params(
            DiscogsSearchRequest(artist="Cat Power", album="Moon Pix", label="Matador")
        )
        assert params["label"] == "Matador"

    def test_omits_label_when_none(self, service):
        params = service._build_search_params(
            DiscogsSearchRequest(artist="Cat Power", album="Moon Pix")
        )
        assert "label" not in params

    @pytest.mark.parametrize("label", ["NULL", "null", ""])
    def test_omits_missing_label_sentinel_values(self, service, label):
        params = service._build_search_params(
            DiscogsSearchRequest(artist="Missy Elliott", album="Supa Dupa Fly", label=label)
        )
        assert "label" not in params

    def test_includes_format_when_provided(self, service):
        params = service._build_search_params(
            DiscogsSearchRequest(artist="Cat Power", album="Moon Pix", format="CD")
        )
        assert params["format"] == "CD"

    def test_omits_format_when_none(self, service):
        params = service._build_search_params(
            DiscogsSearchRequest(artist="Cat Power", album="Moon Pix")
        )
        assert "format" not in params


# ---------------------------------------------------------------------------
# validate_track_on_release
# ---------------------------------------------------------------------------


class TestValidateTrackOnRelease:
    @pytest.mark.asyncio
    async def test_per_track_artist_match(self, service):
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Compilation",
            artist="Various Artists",
            release_url="https://discogs.com/release/1",
            tracklist=[
                TrackItem(position="1", title="My Song", artists=["The Artist"]),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(1, "My Song", "The Artist")
        assert result is True

    @pytest.mark.asyncio
    async def test_release_artist_match(self, service):
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Album",
            artist="Queen",
            release_url="https://discogs.com/release/1",
            tracklist=[
                TrackItem(position="1", title="Bohemian Rhapsody"),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(1, "Bohemian Rhapsody", "Queen")
        assert result is True

    @pytest.mark.asyncio
    async def test_not_found(self, service):
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Album",
            artist="Queen",
            release_url="https://discogs.com/release/1",
            tracklist=[
                TrackItem(position="1", title="Other Song"),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(1, "Missing Song", "Queen")
        assert result is False

    @pytest.mark.asyncio
    async def test_release_not_found(self, service):
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=None):
            result = await service.validate_track_on_release(1, "Song", "Artist")
        assert result is False

    @pytest.mark.asyncio
    async def test_quoted_artist_name_matches(self, service):
        """Discogs formats some artist names with quotes, e.g. '"Weird Al" Yankovic'.

        validate_track_on_release must strip quotes before comparing so that
        the user-supplied 'Weird Al Yankovic' matches the Discogs-formatted
        '"Weird Al" Yankovic'.
        """
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Poodle Hat",
            artist='"Weird Al" Yankovic',
            release_url="https://discogs.com/release/1",
            tracklist=[
                TrackItem(position="1", title="Couch Potato"),
                TrackItem(position="2", title="Hardware Store"),
                TrackItem(position="3", title="Trash Day"),
                TrackItem(position="4", title="Party At The Leper Colony"),
                TrackItem(position="5", title="Angry White Boy Polka"),
                TrackItem(position="6", title="Wanna B Ur Lovr"),
                TrackItem(position="7", title="A Complicated Song"),
                TrackItem(position="8", title="Why Does This Always Happen To Me?"),
                TrackItem(position="9", title="Ode To A Superhero"),
                TrackItem(position="10", title="Bob"),
                TrackItem(position="11", title="Genius In France"),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(1, "Bob", "Weird Al Yankovic")
        assert result is True

    @pytest.mark.asyncio
    async def test_quoted_per_track_artist_matches(self, service):
        """Per-track artists on compilations may also have Discogs quote formatting."""
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Compilation",
            artist="Various Artists",
            release_url="https://discogs.com/release/1",
            tracklist=[
                TrackItem(
                    position="1",
                    title="Bob",
                    artists=['"Weird Al" Yankovic'],
                ),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(1, "Bob", "Weird Al Yankovic")
        assert result is True

    @pytest.mark.asyncio
    async def test_collaboration_trio_release_artist_matches_via_token_set(self, service):
        """Trio collaboration: request artist tokens are a subset of the release artist string.

        A release credited to "Bill Orcutt, Tashi Shelley & Robbie Miller" should validate
        for a request with artist="Orcutt Shelley Miller" (the trio's compact name in WXYC's
        flowsheet). Strict substring match fails because no individual member's name is a
        substring of the trio name. token_set_ratio handles the overlap. See LML#210.
        """
        release = ReleaseMetadataResponse(
            release_id=34993109,
            title="Orcutt Shelley Miller",
            artist="Bill Orcutt, Tashi Shelley & Robbie Miller",
            release_url="https://discogs.com/release/34993109",
            tracklist=[
                TrackItem(position="1", title="A Star Is Born"),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(
                34993109, "A Star Is Born", "Orcutt Shelley Miller"
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_collaboration_trio_per_track_artists_match_via_token_set(self, service):
        """Same trio scenario, but each member is credited as a separate per-track artist."""
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Compilation",
            artist="Various Artists",
            release_url="https://discogs.com/release/1",
            tracklist=[
                TrackItem(
                    position="1",
                    title="A Star Is Born",
                    artists=["Bill Orcutt", "Tashi Shelley", "Robbie Miller"],
                ),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(
                1, "A Star Is Born", "Orcutt Shelley Miller"
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_token_set_fuzzy_does_not_match_unrelated_artist(self, service):
        """The fuzzy fallback must still reject artists with no token overlap."""
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Some Album",
            artist="Bill Orcutt, Tashi Shelley & Robbie Miller",
            release_url="https://discogs.com/release/1",
            tracklist=[
                TrackItem(position="1", title="A Star Is Born"),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(1, "A Star Is Born", "Duke Ellington")
        assert result is False

    @pytest.mark.asyncio
    async def test_diacritics_in_track_title(self, service):
        """Track title with diacritics should match the unaccented search query.

        Discogs stores "Ciências Sensuais" but the user searches for "Ciencias Sensuais".
        """
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Compilation",
            artist="Various Artists",
            release_url="https://discogs.com/release/1",
            tracklist=[
                TrackItem(
                    position="5",
                    title="Ciências Sensuais",
                    artists=["Azul 29"],
                ),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(1, "Ciencias Sensuais", "Azul 29")
        assert result is True

    @pytest.mark.asyncio
    async def test_ampersand_vs_and_in_track_title(self, service):
        """Track 'Me & Mr. Jones' on Discogs should match search for 'Me And Mr Jones'.

        Bug: 'Me And Mr Jones' by Plug failed validation because Discogs stores the
        track as 'Me & Mr. Jones' and the substring check didn't normalize & to 'and'
        or strip periods.
        """
        release = ReleaseMetadataResponse(
            release_id=82602,
            title="Drum 'n' Bass For Papa",
            artist="Plug",
            release_url="https://discogs.com/release/82602",
            tracklist=[
                TrackItem(position="1", title="Me & Mr. Jones"),
                TrackItem(position="2", title="Drum 'n' Bass For Papa"),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(82602, "Me And Mr Jones", "Plug")
        assert result is True

    @pytest.mark.asyncio
    async def test_per_track_credits_fall_back_to_release_artist(self, service):
        """Per-track credits list members/producers, not the band itself.

        Live 93 by The Orb has Towers Of Dub credited to the band members
        (Alex Paterson, Kris Weston, Thomas Fehlmann). When the Discogs API
        surfaces those names under ``track.artists`` (as the cache equivalent
        ``release_track_artist`` does), the validator must still confirm the
        track by falling back to the release-level artist.
        """
        release = ReleaseMetadataResponse(
            release_id=674529,
            title="Live 93",
            artist="The Orb",
            release_url="https://discogs.com/release/674529",
            tracklist=[
                TrackItem(
                    position="5",
                    title="Towers Of Dub",
                    artists=["Alex Paterson", "Kris Weston", "Thomas Fehlmann"],
                ),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(674529, "Towers of Dub", "The Orb")
        assert result is True

    @pytest.mark.asyncio
    async def test_per_track_credits_release_fallback_still_rejects_unrelated(self, service):
        """The release-artist fallback must not match an unrelated artist."""
        release = ReleaseMetadataResponse(
            release_id=674529,
            title="Live 93",
            artist="The Orb",
            release_url="https://discogs.com/release/674529",
            tracklist=[
                TrackItem(
                    position="5",
                    title="Towers Of Dub",
                    artists=["Alex Paterson", "Kris Weston", "Thomas Fehlmann"],
                ),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(674529, "Towers of Dub", "Stereolab")
        assert result is False

    @pytest.mark.asyncio
    async def test_cache_validated(self, service_with_cache):
        service_with_cache.cache_service.validate_track_on_release = AsyncMock(return_value=True)

        result = await service_with_cache.validate_track_on_release(1, "Song", "Artist")
        assert result is True

    @pytest.mark.asyncio
    async def test_memory_cache_hit_skips_api(self, service):
        """Second identical validation should use in-memory cache, not API."""
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Album",
            artist="Stereolab",
            release_url="https://discogs.com/release/1",
            tracklist=[TrackItem(position="1", title="Percolator")],
        )
        mock_get_release = AsyncMock(return_value=release)
        with patch.object(service, "get_release", mock_get_release):
            result1 = await service.validate_track_on_release(1, "Percolator", "Stereolab")
        assert result1 is True
        assert mock_get_release.call_count == 1

        # Second call should use memory cache, not call get_release again
        mock_get_release2 = AsyncMock(return_value=release)
        with patch.object(service, "get_release", mock_get_release2):
            result2 = await service.validate_track_on_release(1, "Percolator", "Stereolab")
        assert result2 is True
        assert mock_get_release2.call_count == 0

    @pytest.mark.asyncio
    async def test_cache_miss_falls_back_to_api(self, service_with_cache):
        service_with_cache.cache_service.validate_track_on_release = AsyncMock(return_value=None)

        release = ReleaseMetadataResponse(
            release_id=1,
            title="Album",
            artist="Queen",
            release_url="https://discogs.com/release/1",
            tracklist=[TrackItem(position="1", title="Song")],
        )
        with patch.object(
            service_with_cache, "get_release", new_callable=AsyncMock, return_value=release
        ):
            result = await service_with_cache.validate_track_on_release(1, "Song", "Queen")
        assert result is True


# ---------------------------------------------------------------------------
# get_artist_image
# ---------------------------------------------------------------------------


class TestGetArtistImage:
    @pytest.mark.asyncio
    async def test_returns_uri(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": 77,
            "name": "Autechre",
            "images": [
                {"uri": "https://i.discogs.com/artist-primary.jpg", "type": "primary"},
                {"uri": "https://i.discogs.com/artist-secondary.jpg", "type": "secondary"},
            ],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_artist_image(77)

        assert result == "https://i.discogs.com/artist-primary.jpg"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_images(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"id": 77, "name": "Autechre", "images": []}

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_artist_image(77)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_api_failure(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock(side_effect=Exception("Not Found"))
        mock_resp.json.return_value = {}

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_artist_image(77)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_rate_limit(self, service):
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=None
        ):
            result = await service.get_artist_image(77)

        assert result is None


# ---------------------------------------------------------------------------
# get_label_image
# ---------------------------------------------------------------------------


class TestGetLabelImage:
    @pytest.mark.asyncio
    async def test_returns_uri(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": 233,
            "name": "Warp Records",
            "images": [{"uri": "https://i.discogs.com/label-logo.jpg", "type": "primary"}],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_label_image(233)

        assert result == "https://i.discogs.com/label-logo.jpg"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_images(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"id": 233, "name": "Warp Records", "images": []}

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_label_image(233)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_api_failure(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock(side_effect=Exception("Not Found"))
        mock_resp.json.return_value = {}

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_label_image(233)

        assert result is None


# ---------------------------------------------------------------------------
# get_release extracts artist_id / label_id
# ---------------------------------------------------------------------------


class TestGetReleaseExtractsIds:
    @pytest.mark.asyncio
    async def test_extracts_artist_and_label_ids(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Confield",
            "artists": [{"id": 77, "name": "Autechre"}],
            "labels": [{"id": 233, "name": "Warp Records"}],
            "tracklist": [],
            "images": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(28138)

        assert result is not None
        assert result.artist_id == 77
        assert result.label_id == 233

    @pytest.mark.asyncio
    async def test_handles_missing_ids(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Confield",
            "artists": [{"name": "Autechre"}],  # no id
            "labels": [],  # no labels
            "tracklist": [],
            "images": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(28138)

        assert result is not None
        assert result.artist_id is None
        assert result.label_id is None


# ---------------------------------------------------------------------------
# get_release parses enriched data (multi-artist, labels, extra artists)
# ---------------------------------------------------------------------------


class TestGetReleaseEnrichedParsing:
    @pytest.mark.asyncio
    async def test_parses_multiple_artists(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Duke Ellington & John Coltrane",
            "artists": [
                {"id": 100, "name": "Duke Ellington", "join": " & "},
                {"id": 101, "name": "John Coltrane", "join": ""},
            ],
            "labels": [{"id": 500, "name": "Impulse Records"}],
            "tracklist": [],
            "images": [],
            "genres": ["Jazz"],
            "styles": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(12345)

        assert result is not None
        assert len(result.artists) == 2
        assert result.artists[0].artist_id == 100
        assert result.artists[0].name == "Duke Ellington"
        assert result.artists[0].join == " & "
        assert result.artists[1].artist_id == 101
        assert result.artists[1].name == "John Coltrane"
        # Backward compat: scalar artist is first artist
        assert result.artist == "Duke Ellington"
        assert result.artist_id == 100

    @pytest.mark.asyncio
    async def test_parses_extra_artists(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Confield",
            "artists": [{"id": 77, "name": "Autechre"}],
            "extraartists": [
                {"id": 200, "name": "Rob Brown", "role": "Producer"},
                {"id": 201, "name": "Sean Booth", "role": "Producer"},
            ],
            "labels": [],
            "tracklist": [],
            "images": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(28138)

        assert result is not None
        assert len(result.extra_artists) == 2
        assert result.extra_artists[0].artist_id == 200
        assert result.extra_artists[0].name == "Rob Brown"
        assert result.extra_artists[0].role == "Producer"

    @pytest.mark.asyncio
    async def test_parses_multiple_labels(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Aluminum Tunes",
            "artists": [{"id": 1, "name": "Stereolab"}],
            "labels": [
                {"id": 233, "name": "Duophonic", "catno": "D-UHF-CD 19"},
                {"id": 400, "name": "Elektra", "catno": "62302-2"},
            ],
            "tracklist": [],
            "images": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(55555)

        assert result is not None
        assert len(result.labels) == 2
        assert result.labels[0].label_id == 233
        assert result.labels[0].name == "Duophonic"
        assert result.labels[0].catno == "D-UHF-CD 19"
        assert result.labels[1].label_id == 400
        # Backward compat: scalar label is first label
        assert result.label == "Duophonic"
        assert result.label_id == 233

    @pytest.mark.asyncio
    async def test_parses_released_date(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Confield",
            "artists": [{"id": 77, "name": "Autechre"}],
            "labels": [],
            "tracklist": [],
            "images": [],
            "genres": [],
            "styles": [],
            "released": "2001-04-30",
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(28138)

        assert result is not None
        assert result.released == "2001-04-30"

    @pytest.mark.asyncio
    async def test_empty_artists_and_labels(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Untitled",
            "artists": [],
            "labels": [],
            "tracklist": [],
            "images": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(99999)

        assert result is not None
        assert result.artists == []
        assert result.extra_artists == []
        assert result.labels == []
        assert result.artist == ""
        assert result.artist_id is None


# ---------------------------------------------------------------------------
# get_artist_details
# ---------------------------------------------------------------------------


class TestGetArtistDetails:
    @pytest.mark.asyncio
    async def test_parses_full_artist_data(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": 77,
            "name": "Autechre",
            "profile": "Electronic duo from Rochdale, England.",
            "namevariations": ["Ae", "Autechre."],
            "aliases": [{"id": 500, "name": "Gescom"}],
            "members": [
                {"id": 200, "name": "Rob Brown", "active": True},
                {"id": 201, "name": "Sean Booth", "active": True},
            ],
            "urls": ["https://autechre.ws", "https://warp.net/artists/autechre"],
            "images": [
                {"uri": "https://i.discogs.com/autechre.jpg", "type": "primary"},
            ],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_artist_details(77)

        assert result is not None
        assert result.artist_id == 77
        assert result.name == "Autechre"
        assert result.profile == "Electronic duo from Rochdale, England."
        assert result.image_url == "https://i.discogs.com/autechre.jpg"
        assert result.name_variations == ["Ae", "Autechre."]
        assert len(result.aliases) == 1
        assert result.aliases[0].id == 500
        assert result.aliases[0].name == "Gescom"
        assert len(result.members) == 2
        assert result.members[0].name == "Rob Brown"
        assert result.urls == ["https://autechre.ws", "https://warp.net/artists/autechre"]

    @pytest.mark.asyncio
    async def test_handles_minimal_response(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": 1,
            "name": "Unknown Artist",
            "images": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_artist_details(1)

        assert result is not None
        assert result.artist_id == 1
        assert result.name == "Unknown Artist"
        assert result.profile is None
        assert result.image_url is None
        assert result.name_variations == []
        assert result.aliases == []
        assert result.members == []
        assert result.urls == []

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self, service):
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=None
        ):
            result = await service.get_artist_details(77)

        assert result is None

    @pytest.mark.asyncio
    async def test_cache_hit(self, service_with_cache):
        from datetime import datetime

        from discogs.models import ArtistDetails

        # `fetched_at` non-NULL marks the row as fully fetched — the
        # cache-hit discriminator added in #502. Without it the seam
        # treats the row as a rebuild-stub and falls through to the API.
        cached_details = ArtistDetails(
            artist_id=77,
            name="Autechre",
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
            cached=True,
        )
        service_with_cache.cache_service.get_artist_details = AsyncMock(return_value=cached_details)

        result = await service_with_cache.get_artist_details(77)
        assert result is not None
        assert result.artist_id == 77
        assert result.cached is True

    @pytest.mark.asyncio
    async def test_cache_hit_when_fetched_but_no_profile(self, service_with_cache):
        """Discogs answered, no profile text — still a hit, no re-fetch.

        Guards against accidentally treating every Discogs-confirmed-empty
        profile as a permanent miss, which would cost a round-trip per
        flowsheet lookup against the no-profile tail. See #502.
        """
        from datetime import datetime

        from discogs.models import ArtistDetails

        cached_details = ArtistDetails(
            artist_id=77,
            name="Yetsuby",
            profile=None,
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
            cached=True,
        )
        service_with_cache.cache_service.get_artist_details = AsyncMock(return_value=cached_details)
        service_with_cache.cache_service.write_artist_details = AsyncMock()

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
        ) as request_mock:
            result = await service_with_cache.get_artist_details(77)

        assert result is not None
        assert result.cached is True
        request_mock.assert_not_called()
        service_with_cache.cache_service.write_artist_details.assert_not_called()

    @pytest.mark.asyncio
    async def test_stub_row_falls_through_to_api(self, service_with_cache):
        """A stub row (fetched_at IS NULL, profile IS NULL — created by the
        monthly rebuild's stub-from-release_artist path) is treated as a
        cache miss. The seam fires `_api_fetch` + write-back so subsequent
        calls return populated data. See #502 and #497.
        """
        from discogs.models import ArtistDetails

        stub = ArtistDetails(
            artist_id=6998498,
            name="Yetsuby",
            profile=None,
            fetched_at=None,
            cached=True,
        )
        service_with_cache.cache_service.get_artist_details = AsyncMock(return_value=stub)
        service_with_cache.cache_service.write_artist_details = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": 6998498,
            "name": "Yetsuby",
            "profile": "Live show.",
            "images": [],
        }

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ) as request_mock:
            result = await service_with_cache.get_artist_details(6998498)

        request_mock.assert_awaited_once()
        service_with_cache.cache_service.write_artist_details.assert_called_once()
        assert result is not None
        assert result.profile == "Live show."

    @pytest.mark.asyncio
    async def test_writes_back_to_cache(self, service_with_cache):
        service_with_cache.cache_service.get_artist_details = AsyncMock(return_value=None)
        service_with_cache.cache_service.write_artist_details = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": 77,
            "name": "Autechre",
            "images": [],
        }

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            await service_with_cache.get_artist_details(77)

        service_with_cache.cache_service.write_artist_details.assert_called_once()


# ---------------------------------------------------------------------------
# get_artist_image delegates to get_artist_details
# ---------------------------------------------------------------------------


class TestGetArtistImageDelegation:
    @pytest.mark.asyncio
    async def test_delegates_to_get_artist_details(self, service):
        from discogs.models import ArtistDetails

        details = ArtistDetails(
            artist_id=77,
            name="Autechre",
            image_url="https://i.discogs.com/autechre.jpg",
        )
        with patch.object(
            service, "get_artist_details", new_callable=AsyncMock, return_value=details
        ):
            result = await service.get_artist_image(77)

        assert result == "https://i.discogs.com/autechre.jpg"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_details(self, service):
        with patch.object(service, "get_artist_details", new_callable=AsyncMock, return_value=None):
            result = await service.get_artist_image(77)

        assert result is None


# ---------------------------------------------------------------------------
# get_master
# ---------------------------------------------------------------------------


class TestGetMaster:
    @pytest.mark.asyncio
    async def test_api_success(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": 456,
            "title": "Dots and Loops",
            "year": 1997,
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_master(456)

        assert result is not None
        assert result.master_id == 456
        assert result.title == "Dots and Loops"
        assert result.year == 1997
        assert result.cached is False

    @pytest.mark.asyncio
    async def test_not_found_returns_none(self, service):
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=None
        ):
            result = await service.get_master(999999)
        assert result is None

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status = MagicMock(side_effect=Exception("Server error"))

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_master(456)
        assert result is None
